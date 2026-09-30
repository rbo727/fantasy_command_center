"""FantasyGuru subscriber content.

Two separable halves, on purpose:

* :class:`FantasyGuruSession` logs in with Playwright and returns raw HTML. It
  caches the session cookie so a normal week costs one page fetch, not a login.
* :func:`extract_staff_picks` turns that HTML into structured picks with one
  Claude call. It takes HTML as an argument and never touches the network, so it
  is testable against saved pages and a page-layout change can be diagnosed
  without logging in.

Raw HTML is cached before parsing. A parser bug should never cost a re-fetch,
and when the site is redesigned mid-season the cached page is the evidence.

This reads a personal subscription with the subscriber's own credentials, at
human rates, and caches aggressively to stay a polite single client. The
content is not redistributed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fcc.core.config import get_settings
from fcc.core.secrets import Keys, SecretStore
from fcc.engines.pickem import StaffPick
from fcc.engines.value import RankedPlayer
from fcc.sources.html_text import html_to_text

log = logging.getLogger(__name__)

BASE_URL = "https://www.fantasyguru.com"
LOGIN_URL = f"{BASE_URL}/account/login"

#: The session cookie the site sets on a successful sign-in. Its presence is the
#: positive signal that we are logged in - scanning page text for "please log
#: in" wording let a paywalled page pass as logged-in, because the site says
#: "Unlock the rankings" instead.
AUTH_COOKIE = "fg_auth_token"

#: How long a cached page stays fresh. Staff picks move during the week, so this
#: is short enough to pick up Sunday-morning updates.
CACHE_TTL_SECONDS = 60 * 30

#: Jeff Mans' weekly board, PPR scoring - matches the Sleeper league's format.
RANKINGS_URL = f"{BASE_URL}/jeff-mans-nfl-weekly-rankings-ppr"
#: The board's position filter values, in the order they're exported.
RANKING_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DST")

#: Weekly offensive projections - per-player stat lines with PPR/half/non points.
PROJECTIONS_URL = f"{BASE_URL}/nfl-projections-weekly-off"


class FantasyGuruError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Extraction (no network; testable against saved HTML)
# --------------------------------------------------------------------------
class ExtractedPick(BaseModel):
    """One staff pick, as the model reads it off the page."""

    matchup: str = Field(description="The game, e.g. 'BAL@KC' or 'Ravens at Chiefs'")
    team: str = Field(description="The team the staff favour, as written on the page")
    market: Literal["ATS", "ML", "SU", "OU", "UNKNOWN"] = Field(
        description=(
            "ATS = against the spread, ML = moneyline, SU = straight-up winner, "
            "OU = total points over/under. Use UNKNOWN if the page does not say."
        )
    )
    spread: float | None = Field(
        default=None,
        description=(
            "Point spread from the picked team's perspective: negative if they are "
            "favoured (laying points), positive if they are an underdog. Null if absent."
        ),
    )
    conviction: int = Field(
        default=3,
        ge=1,
        le=5,
        description="How strongly the staff back this pick, 1 (lean) to 5 (best bet).",
    )
    rationale: str = Field(default="", description="Their stated reasoning, briefly.")


class ExtractedPicks(BaseModel):
    picks: list[ExtractedPick] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="Anything ambiguous or unreadable on the page that a human should check.",
    )


EXTRACTION_SYSTEM = """\
You extract betting and pick'em recommendations from a fantasy football analysis page.

Rules:
- Only extract picks the staff actually make. Ignore odds tables, ads, general
  analysis, and other people's picks quoted for contrast.
- The `spread` is from the picked team's point of view: a team laying 6.5 is
  -6.5, a team getting 6.5 is +6.5. This sign convention matters more than
  anything else on the page - a flipped sign inverts the pick downstream.
- Set `market` to what the page actually says. Do not assume a spread pick is a
  moneyline pick. If the market is genuinely unstated, use UNKNOWN rather than
  guessing.
- Use the team names as written. Do not normalise or abbreviate them.
- If the page looks like a login wall, a paywall, or an error page rather than
  picks, return an empty list and say so in `notes`.
"""


def extract_staff_picks(
    html: str,
    week: int | None = None,
    client=None,
    model: str | None = None,
) -> tuple[list[StaffPick], str]:
    """Structure a staff-picks page into :class:`StaffPick` objects.

    Returns ``(picks, notes)``. This is the one genuinely fuzzy step in the
    pipeline, which is why it is the only place an LLM is used: everything
    downstream — team resolution, market translation, submission — is
    deterministic and tested.
    """
    settings = get_settings()
    if client is None:
        # Imported lazily so an injected client (tests, or a different
        # transport) doesn't require the SDK to be installed.
        import anthropic

        client = anthropic.Anthropic(api_key=SecretStore().get(Keys.ANTHROPIC_API_KEY) or None)
    text = html_to_text(html)
    if not text.strip():
        raise FantasyGuruError("Page contained no readable text after HTML stripping.")

    week_hint = f"This is week {week}. " if week else ""
    response = client.messages.parse(
        model=model or settings.anthropic_model,
        max_tokens=16000,
        system=EXTRACTION_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{week_hint}Extract every staff pick from this page."
                    f"\n\n<page>\n{text}\n</page>"
                ),
            }
        ],
        output_format=ExtractedPicks,
    )
    parsed: ExtractedPicks = response.parsed_output
    picks = [
        StaffPick(
            matchup=p.matchup,
            team=p.team,
            market=p.market,
            spread=p.spread,
            conviction=p.conviction,
            rationale=p.rationale,
        )
        for p in parsed.picks
    ]
    log.info("extracted %d staff picks (notes: %s)", len(picks), parsed.notes or "none")
    return picks, parsed.notes


class ExtractedRanking(BaseModel):
    """One player from a rankings or projections page."""

    player: str = Field(description="Player name exactly as written on the page")
    position: str = Field(description="QB, RB, WR, TE, K or DEF")
    projected_points: float | None = Field(
        default=None,
        description=(
            "Projected fantasy points PER GAME for the period the page covers. "
            "If the page shows a season or rest-of-season total, divide by the "
            "games remaining and say so in notes. Null if the page shows no "
            "points at all."
        ),
    )
    positional_rank: int | None = Field(
        default=None, description="Rank within the position, 1 = best. Null if absent."
    )
    overall_rank: int | None = Field(default=None, description="Overall rank if shown.")
    tier: int | None = Field(default=None, description="Tier number if the page uses tiers.")


class ExtractedRankings(BaseModel):
    scope: Literal["rest_of_season", "weekly", "season_total", "unknown"] = Field(
        description="What period these rankings cover."
    )
    players: list[ExtractedRanking] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description=(
            "Anything a human should check: whether points were per-game or "
            "converted from a total, scoring format, or an unreadable page."
        ),
    )


RANKINGS_SYSTEM = """\
You extract fantasy football player rankings and projections from an analysis page.

Rules:
- Extract every ranked player you can see, not just the top few.
- `projected_points` must be PER GAME. If the page gives a rest-of-season or
  season total, divide by the number of games it covers and note that you did.
  A total silently treated as a per-game figure would inflate every downstream
  valuation by an order of magnitude.
- Use the position labels as the page gives them, normalised to QB/RB/WR/TE/K/DEF.
- If the page shows ranks but no projected points, leave projected_points null.
  Do not invent points from a rank.
- If this looks like a login wall, a paywall, or an index page rather than
  rankings, return an empty list and say so in `notes`.
"""


def extract_rankings(
    html: str,
    client=None,
    model: str | None = None,
) -> tuple[list[RankedPlayer], str, str]:
    """Structure a rankings page into ranked players.

    Returns ``(players, scope, notes)``. Like the staff-pick extractor this
    takes HTML and never touches the network, so a page-layout change can be
    diagnosed from a saved file without logging in.
    """
    settings = get_settings()
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=SecretStore().get(Keys.ANTHROPIC_API_KEY) or None)

    text = html_to_text(html)
    if not text.strip():
        raise FantasyGuruError("Page contained no readable text after HTML stripping.")

    response = client.messages.parse(
        model=model or settings.anthropic_model,
        max_tokens=16000,
        system=RANKINGS_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Extract every ranked player from this page."
                f"\n\n<page>\n{text}\n</page>",
            }
        ],
        output_format=ExtractedRankings,
    )
    parsed: ExtractedRankings = response.parsed_output
    players = [
        RankedPlayer(
            name=p.player,
            position=(p.position or "").upper(),
            projected_points=p.projected_points,
            positional_rank=p.positional_rank,
            overall_rank=p.overall_rank,
        )
        for p in parsed.players
    ]
    log.info(
        "extracted %d ranked players (scope=%s, notes: %s)",
        len(players), parsed.scope, parsed.notes or "none",
    )
    return players, parsed.scope, parsed.notes


# --------------------------------------------------------------------------
# Rankings board (CSV export - no LLM needed)
# --------------------------------------------------------------------------
def parse_rankings_csv(text: str, position: str) -> list[RankedPlayer]:
    """One position's CSV export into ranked players. Pure - no network.

    The board carries ranks, not projected points, so ``projected_points``
    stays ``None``: a rank is not a point total, and inventing one from it
    would feed made-up numbers into the value math.
    """
    reader = csv.DictReader(io.StringIO(text))
    header = {(h or "").strip().upper() for h in reader.fieldnames or []}
    if not {"RANK", "PLAYER", "TEAM"} <= header:
        raise FantasyGuruError(f"Rankings CSV columns changed: {reader.fieldnames}")

    pos = "DEF" if position.upper() in ("DST", "DEF") else position.upper()
    out: list[RankedPlayer] = []
    for raw in reader:
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()}
        try:
            rank = int(row["RANK"])
        except (KeyError, ValueError):
            continue
        if not row.get("PLAYER"):
            continue
        out.append(
            RankedPlayer(
                name=row["PLAYER"], position=pos, positional_rank=rank,
                team=row.get("TEAM") or None,
            )
        )
    return out


def rankings_cache_path() -> Path:
    return get_settings().data_path / "cache" / "fantasyguru" / "rankings.json"


def save_rankings(csvs: dict[str, str], url: str = RANKINGS_URL) -> dict:
    """Parse and persist a fetched board; the raw CSVs are kept as evidence."""
    board = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "url": url,
        "positions": {
            ("DEF" if pos == "DST" else pos): [
                {"name": p.name, "team": p.team, "rank": p.positional_rank}
                for p in parse_rankings_csv(text, pos)
            ]
            for pos, text in csvs.items()
        },
        "raw_csv": csvs,
    }
    path = rankings_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=1))
    return board


def load_rankings() -> dict | None:
    path = rankings_cache_path()
    return json.loads(path.read_text()) if path.exists() else None


# --------------------------------------------------------------------------
# Weekly projections (CSV export)
# --------------------------------------------------------------------------
#: Board column for each scoring format, keyed the way the Sleeper side names them.
PROJECTION_COLUMNS = {"ppr": "PPR", "half_ppr": "HALF", "std": "NON"}


def parse_projections_csv(text: str) -> list[dict]:
    """The projections export as ``{position, name, team, opp, ppr, half_ppr, std}`` rows."""
    reader = csv.DictReader(io.StringIO(text))
    header = {(h or "").strip().upper() for h in reader.fieldnames or []}
    needed = {"POS", "PLAYER", "TEAM", *PROJECTION_COLUMNS.values()}
    if not needed <= header:
        raise FantasyGuruError(f"Projections CSV columns changed: {reader.fieldnames}")

    out = []
    for raw in reader:
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()}
        if not row.get("PLAYER") or not row.get("POS"):
            continue
        points = {}
        for key, column in PROJECTION_COLUMNS.items():
            try:
                points[key] = float(row[column])
            except ValueError:
                points[key] = None
        out.append({
            "position": row["POS"].upper(), "name": row["PLAYER"],
            "team": row.get("TEAM") or None, "opp": row.get("OPP") or None, **points,
        })
    if not out:
        raise FantasyGuruError("Projections CSV had no players.")
    return out


def projections_cache_path() -> Path:
    return get_settings().data_path / "cache" / "fantasyguru" / "projections.json"


def save_projections(text: str, url: str = PROJECTIONS_URL) -> dict:
    saved = {
        "fetched_at": datetime.now(UTC).isoformat(), "url": url,
        "players": parse_projections_csv(text), "raw_csv": text,
    }
    path = projections_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=1))
    return saved


def load_projections() -> dict | None:
    path = projections_cache_path()
    return json.loads(path.read_text()) if path.exists() else None


# --------------------------------------------------------------------------
# Weekly waiver report (fixed line format - no LLM needed)
# --------------------------------------------------------------------------
#: The NFL hub page lists the current week's articles, including the report.
NFL_HUB_URL = f"{BASE_URL}/nfl-seasonal"

_WAIVER_LINK = re.compile(r'href="/(?P<slug>(?P<season>\d{4})-week-(?P<week>\d+)-waiver-report)"')

_POSITION_HEADINGS = {
    "quarterbacks": "QB",
    "running backs": "RB",
    "wide receivers": "WR",
    "tight ends": "TE",
    "kickers": "K",
    "defense / special teams": "DEF",
    "defense/special teams": "DEF",
    "team defenses": "DEF",
}

_DASH = r"\s*[—–-]+\s*"
#: "Jacoby Brissett, ARI (10% Owned Yahoo) — FAAB Recommendation: 0-2%"
#: The team is sometimes omitted ("Kalif Raymond (10% Owned Yahoo) – ...").
_TARGET = re.compile(
    r"^(?P<name>[^(]+?)(?:,\s*(?P<team>[A-Z]{2,3}))?\s*\((?P<owned>\d+)% Owned[^)]*\)"
    + _DASH + r"(?P<rest>.*)$"
)
_FAAB = re.compile(r"FAAB Recommendation:\s*(?P<lo>\d+)(?:\s*[–-]\s*(?P<hi>\d+))?\s*%", re.I)
#: "Kenyon Sadiq, TE, NYJ (15% FAAB)" under "Favorite Priority Adds".
_PRIORITY = re.compile(
    r"^(?P<name>.+?),\s*(?P<pos>[A-Z]{1,3}),\s*(?P<team>[A-Z]{2,3})\s*"
    r"\((?P<lo>\d+)(?:\s*[–-]\s*(?P<hi>\d+))?% FAAB\)$"
)
#: "Buffalo Bills vs NE – I was giving Maye a pass..."
_DEFENSE = re.compile(
    r"^(?P<name>[A-Z][\w .'’]+?) (?P<where>vs|at|@) (?P<opp>[A-Z]{2,3})"
    + _DASH + r"(?P<note>.+)$"
)


def find_waiver_report_url(hub_html: str) -> str | None:
    """The newest ``<season>-week-<n>-waiver-report`` linked from the hub."""
    found = [(int(m["season"]), int(m["week"]), m["slug"]) for m in _WAIVER_LINK.finditer(hub_html)]
    return f"{BASE_URL}/{max(found)[2]}" if found else None


def _faab(lo: str | None, hi: str | None) -> dict:
    low = int(lo) if lo is not None else None
    return {"faab_pct_low": low, "faab_pct_high": int(hi) if hi is not None else low}


def parse_waiver_report(html: str) -> dict:
    """Structure the weekly waiver report. Pure - no network.

    Recommendations are written one per line in a fixed format, so they're
    read with patterns rather than a model. Any line that *looks* like a
    recommendation but doesn't fit is returned in ``unparsed`` instead of
    being dropped, so a format change shows up on the dashboard rather than
    as a quietly shorter list.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in html_to_text(html).splitlines()]
    lines = [ln for ln in lines if ln]

    report: dict = {
        "title": None, "week": None, "priority": [], "targets": [], "defenses": [],
        "unparsed": [],
    }
    position: str | None = None
    tier = "target"
    mode = "preamble"
    #: Picks still waiting for a write-up. Teammates often share one paragraph
    #: ("Ollie Gordon and Jaylen Wright..."), so it belongs to all of them.
    unwritten: list[dict] = []

    for line in lines:
        lower = line.lower().rstrip(":")
        title = re.search(r"week (\d+) waiver report$", lower)
        if report["title"] is None and title:
            report["title"], report["week"] = line, int(title.group(1))
            continue
        if lower.endswith("waiver targets"):
            mode = "targets"
            continue
        if mode == "preamble":
            continue
        if lower in _POSITION_HEADINGS:
            position, tier, mode = _POSITION_HEADINGS[lower], "target", "targets"
            unwritten = []
            continue
        if lower.startswith("names to consider"):
            tier, unwritten = "deeper", []
            continue
        if lower == "favorite priority adds":
            mode, unwritten = "priority", []
            continue
        if lower == "related articles" or lower.startswith("if you have any specific"):
            break

        if mode == "priority":
            m = _PRIORITY.match(line)
            if m:
                report["priority"].append({
                    "name": m["name"].strip(), "position": m["pos"], "team": m["team"],
                    **_faab(m["lo"], m["hi"]),
                })
            elif "faab" in lower:
                report["unparsed"].append(line)
            continue

        m = _TARGET.match(line)
        if m:
            faab = _FAAB.search(m["rest"])
            pick = {
                "position": position, "tier": tier, "name": m["name"].strip(),
                "team": m["team"], "owned_pct": int(m["owned"]),
                **(_faab(faab["lo"], faab["hi"]) if faab else _faab(None, None)),
                "blurb": "" if faab else m["rest"].strip(),
            }
            report["targets"].append(pick)
            unwritten = [] if pick["blurb"] else [*unwritten, pick]
            continue
        if position == "DEF" and (d := _DEFENSE.match(line)):
            report["defenses"].append({
                "name": d["name"].strip(), "where": d["where"], "opp": d["opp"],
                "note": d["note"].strip(),
            })
            unwritten = []
            continue
        if "faab recommendation" in lower or "% owned" in lower:
            report["unparsed"].append(line)
            continue
        # Prose following a recommendation is its write-up; keep the first
        # paragraph, which is where the case for the player is made.
        for pick in unwritten:
            pick["blurb"] = line
        unwritten = []

    if not report["targets"] and not report["priority"]:
        raise FantasyGuruError(
            "No recommendations found in the waiver report - the page layout may have changed."
        )
    return report


def waiver_report_cache_path() -> Path:
    return get_settings().data_path / "cache" / "fantasyguru" / "waiver_report.json"


def save_waiver_report(report: dict, url: str) -> dict:
    saved = {**report, "url": url, "fetched_at": datetime.now(UTC).isoformat()}
    path = waiver_report_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=1))
    return saved


def load_waiver_report() -> dict | None:
    path = waiver_report_cache_path()
    return json.loads(path.read_text()) if path.exists() else None


@contextmanager
def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise FantasyGuruError(
            "Playwright is not installed. `pip install playwright && "
            "playwright install chromium`."
        ) from exc
    with sync_playwright() as pw:
        yield pw


# --------------------------------------------------------------------------
# Authenticated fetch (Playwright)
# --------------------------------------------------------------------------
class FantasyGuruSession:
    """Logs in once, reuses the cookie, and caches fetched pages."""

    def __init__(self, store: SecretStore | None = None) -> None:
        self.settings = get_settings()
        self.store = store or SecretStore()
        self.cache_dir = self.settings.data_path / "cache" / "fantasyguru"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- caching ---------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"{digest}.html"

    def _cached(self, url: str, max_age: int) -> str | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        age = datetime.now(UTC).timestamp() - path.stat().st_mtime
        if age > max_age:
            return None
        log.debug("using cached %s (%.0fs old)", url, age)
        return path.read_text()

    def _store_cache(self, url: str, html: str) -> Path:
        path = self._cache_path(url)
        path.write_text(html)
        (self.cache_dir / "index.json").write_text(
            json.dumps({self._cache_path(url).name: url}, indent=2)
        )
        return path

    # -- cookies ---------------------------------------------------------
    def _load_cookies(self) -> list[dict] | None:
        raw = self.store.get(Keys.FANTASYGURU_COOKIES)
        return json.loads(raw) if raw else None

    def _save_cookies(self, cookies: list[dict]) -> None:
        self.store.set(Keys.FANTASYGURU_COOKIES, json.dumps(cookies))

    # -- fetching --------------------------------------------------------
    def fetch(self, url: str, max_age: int = CACHE_TTL_SECONDS, force: bool = False) -> str:
        """Return the HTML for *url*, logging in only if the cookie is stale."""
        if not force:
            cached = self._cached(url, max_age)
            if cached is not None:
                return cached

        with _playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self._logged_in_page(browser)
                self._open(page, url)
                html = page.content()
            finally:
                browser.close()

        self._store_cache(url, html)
        return html

    def fetch_rankings(
        self, url: str = RANKINGS_URL, positions: tuple[str, ...] = RANKING_POSITIONS
    ) -> dict[str, str]:
        """Each position's CSV export from a rankings board, as ``{position: csv}``.

        The board shows one position at a time behind a ``<select>``, and its
        CSV button exports the whole filtered list across every page. Each
        export is checked against the first row the page is showing, so a
        filter that silently failed to apply can't pass one position's list
        off as another's.
        """
        out: dict[str, str] = {}
        with _playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self._logged_in_page(browser)
                self._open(page, url)
                select = page.locator("main select:has(option[value='QB'])").first
                for pos in positions:
                    select.select_option(pos)
                    page.wait_for_load_state("networkidle", timeout=30_000)
                    shown = page.locator("main table tbody tr td").nth(1).inner_text().strip()
                    with page.expect_download(timeout=30_000) as dl:
                        page.get_by_role("button", name="CSV", exact=True).click()
                    text = Path(dl.value.path()).read_text()
                    first = next(iter(parse_rankings_csv(text, pos)), None)
                    if first is None or first.name != shown:
                        raise FantasyGuruError(
                            f"{pos} export doesn't match the board (page shows {shown!r}, "
                            f"CSV starts {first.name if first else None!r}). The position "
                            "filter may have changed."
                        )
                    if text in out.values():
                        raise FantasyGuruError(f"{pos} export is identical to another position's.")
                    out[pos] = text
            finally:
                browser.close()
        return out

    def fetch_projections(self, url: str = PROJECTIONS_URL) -> str:
        """The weekly projections board as one CSV (every position, every page)."""
        with _playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self._logged_in_page(browser)
                self._open(page, url)
                # "" is the board's all-positions filter; set it explicitly so
                # a remembered filter can't quietly narrow the export.
                page.locator("main select:has(option[value='QB'])").first.select_option("")
                page.wait_for_load_state("networkidle", timeout=30_000)
                with page.expect_download(timeout=30_000) as dl:
                    page.get_by_role("button", name="CSV", exact=True).click()
                return Path(dl.value.path()).read_text()
            finally:
                browser.close()

    def _open(self, page, url: str) -> None:
        """Navigate to a subscriber page, re-logging in once if it's still locked."""
        # The site renders content client-side, so wait for the network to
        # settle rather than for the bare HTML shell.
        page.goto(url, wait_until="networkidle", timeout=60_000)
        if self._looks_locked(self._main_text(page)):
            # A stored cookie can outlive its server-side session. Drop it, log
            # in once more, then accept whatever the page says.
            log.info("page locked with a stored session; logging in again")
            page.context.clear_cookies()
            self._login(page)
            self._save_cookies(page.context.cookies())
            page.goto(url, wait_until="networkidle", timeout=60_000)
        if self._looks_locked(self._main_text(page)):
            raise FantasyGuruError(
                f"{url} is still behind the paywall while logged in. The subscription "
                "may not include this page, or the session was rejected."
            )

    def _logged_in_page(self, browser):
        """A page in a context holding a valid session, logging in if needed."""
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        cookies = self._load_cookies()
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        if not self._has_session(context):
            log.info("no FantasyGuru session cookie; logging in")
            self._login(page)
            self._save_cookies(context.cookies())
        return page

    @staticmethod
    def _has_session(context) -> bool:
        return any(c["name"] == AUTH_COOKIE for c in context.cookies())

    def _login(self, page) -> None:
        username = self.store.require(Keys.FANTASYGURU_USERNAME)
        password = self.store.require(Keys.FANTASYGURU_PASSWORD)
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)
        page.fill("input[name='email']", username)
        page.fill("input[name='password']", password)
        page.click("form button[type='submit'], form button:has-text('Sign in')")
        # The submit is an async server action: "network idle" can arrive before
        # the cookie does. Wait for the cookie itself, which is what success means.
        for _ in range(60):
            if self._has_session(page.context):
                return
            page.wait_for_timeout(500)
        raise FantasyGuruError(
            "Sign-in produced no session cookie. Check the stored FantasyGuru "
            "credentials, or whether the login page changed."
        )

    @staticmethod
    def _main_text(page) -> str:
        """Rendered text of the page's main content, not the site chrome.

        Membership promos in the sidebar and footer say "Already a member?
        Sign in" on every page, logged in or not, so reading the whole body
        makes an unlocked page look paywalled.
        """
        main = page.query_selector("main") or page.query_selector("body")
        return main.inner_text() if main else ""

    @staticmethod
    def _looks_locked(text: str) -> bool:
        """The paywall's own call to action, e.g. "UNLOCK THE RANKINGS"."""
        text = text.lower()
        markers = ("unlock the", "sign in to continue", "subscribe to read", "please log in")
        return any(m in text for m in markers)
