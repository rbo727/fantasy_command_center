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

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fcc.core.config import get_settings
from fcc.core.secrets import Keys, SecretStore
from fcc.engines.pickem import StaffPick
from fcc.sources.html_text import html_to_text

log = logging.getLogger(__name__)

BASE_URL = "https://www.fantasyguru.com"
LOGIN_URL = f"{BASE_URL}/login"

#: How long a cached page stays fresh. Staff picks move during the week, so this
#: is short enough to pick up Sunday-morning updates.
CACHE_TTL_SECONDS = 60 * 30


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

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise FantasyGuruError(
                "Playwright is not installed. `pip install playwright && "
                "playwright install chromium`."
            ) from exc

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
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
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            if self._looks_logged_out(page.content()):
                log.info("FantasyGuru session stale; logging in")
                self._login(page)
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                self._save_cookies(context.cookies())

            html = page.content()
            browser.close()

        if self._looks_logged_out(html):
            raise FantasyGuruError(
                "Still logged out after a login attempt. Check the stored "
                "FantasyGuru credentials, or whether the login page changed."
            )
        self._store_cache(url, html)
        return html

    def _login(self, page) -> None:
        username = self.store.require(Keys.FANTASYGURU_USERNAME)
        password = self.store.require(Keys.FANTASYGURU_PASSWORD)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        # Selectors are intentionally broad: this is the part most likely to
        # break on a redesign, and a broad match fails loudly rather than subtly.
        page.fill("input[type='email'], input[name='username'], input[name='email']", username)
        page.fill("input[type='password'], input[name='password']", password)
        page.click("button[type='submit'], input[type='submit']")
        page.wait_for_load_state("networkidle", timeout=45_000)

    @staticmethod
    def _looks_logged_out(html: str) -> bool:
        text = html_to_text(html).lower()
        markers = (
            "sign in to continue",
            "subscribe to read",
            "please log in",
            "start your subscription",
        )
        return any(m in text for m in markers)
