"""ESPN pick'em client (the "gambit" API).

ESPN's pick'em games live on a separate API from ESPN fantasy:
``gambit-api.fantasy.espn.com``. The read endpoints are well known from the
community's endpoint catalogues; the **write** path is not published anywhere,
so it is supplied at runtime from a request you capture once in DevTools (see
:class:`WriteSpec` and ``fcc pickem capture-write``).

Auth is the same cookie pair as ESPN fantasy: ``SWID`` and ``espn_s2``.

A note on the parsers below: ESPN returns large, deeply-nested documents whose
exact field names vary by challenge and season. Everything here is written
defensively and every extractor tries several known field spellings, because the
alternative — assuming one shape — fails silently and submits garbage. Run
``fcc pickem dump`` against your real account to capture live fixtures and
tighten these once you have them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from fcc.core.teams import normalize

log = logging.getLogger(__name__)

BASE = "https://gambit-api.fantasy.espn.com/apis/v1"

#: Challenge slugs follow a stable pattern; the season suffix changes yearly.
CHALLENGE_TEMPLATES = {
    "nfl_pickem": "nfl-pickem-{season}",
    "nfl_pigskin": "nfl-pigskin-pickem-{season}",
    "college_pickem": "college-football-pickem-{season}",
}


def challenge_slug(kind: str, season: int) -> str:
    if kind in CHALLENGE_TEMPLATES:
        return CHALLENGE_TEMPLATES[kind].format(season=season)
    # Allow a literal slug to pass through, for challenges we haven't templated.
    return kind


class PickemError(RuntimeError):
    pass


class WriteNotConfigured(PickemError):
    """Raised when we know what to pick but not yet how to send it."""


def _first(d: dict, *names: str, default: Any = None) -> Any:
    """Return the first present, non-None key. ESPN renames fields between games."""
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        # ESPN uses epoch milliseconds in most gambit payloads.
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class Option:
    """One selectable side of a proposition."""

    id: str
    team_code: str | None
    label: str
    pick_percentage: float | None = None

    @property
    def resolved(self) -> bool:
        return self.team_code is not None


@dataclass
class Proposition:
    """One pickable game."""

    id: str
    week: int | None
    options: list[Option] = field(default_factory=list)
    lock_time: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def team_codes(self) -> list[str]:
        return [o.team_code for o in self.options if o.team_code]

    @property
    def fully_resolved(self) -> bool:
        """True only if every side maps to exactly one known NFL team."""
        codes = self.team_codes
        return len(self.options) == len(codes) == len(set(codes)) >= 2

    def option_for(self, team_code: str) -> Option | None:
        for o in self.options:
            if o.team_code == team_code:
                return o
        return None

    @property
    def label(self) -> str:
        return " vs ".join(o.team_code or o.label for o in self.options)

    def is_locked(self, now: datetime | None = None) -> bool:
        if self.lock_time is None:
            return False
        return (now or datetime.now(UTC)) >= self.lock_time


@dataclass
class Pick:
    """A decision to send: which option of which proposition, at what weight."""

    proposition_id: str
    option_id: str
    team_code: str
    #: Confidence-point value, for pools that use them. None for straight pick'em.
    confidence: int | None = None


@dataclass
class WriteSpec:
    """A pick-submission request captured from the browser.

    ESPN does not document how picks are submitted, so rather than guess we
    replay the shape of a request you made yourself. Capture it once:

    1. Open your pick'em entry, DevTools → Network, filter ``gambit``.
    2. Make one pick and submit.
    3. Right-click the resulting POST/PUT → *Copy as cURL*.
    4. ``fcc pickem capture-write --curl-file cmd.txt``

    ``body_template`` is JSON with ``{picks}`` where the pick array belongs.
    """

    method: str
    url: str
    headers: dict[str, str]
    body_template: str
    #: Field names used inside each pick object in the captured body.
    proposition_field: str = "propositionId"
    option_field: str = "outcomeId"
    confidence_field: str = "confidencePoints"

    @classmethod
    def load(cls, path: Path) -> WriteSpec | None:
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2))

    def render_body(self, picks: list[Pick]) -> str:
        items = []
        for p in picks:
            item = {self.proposition_field: p.proposition_id, self.option_field: p.option_id}
            if p.confidence is not None:
                item[self.confidence_field] = p.confidence
            items.append(item)
        return self.body_template.replace("{picks}", json.dumps(items))


class ESPNPickemClient:
    def __init__(
        self,
        swid: str,
        espn_s2: str,
        challenge: str,
        write_spec: WriteSpec | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        # ESPN wants SWID wrapped in braces; accept either form from the user.
        self.swid = swid if swid.startswith("{") else "{" + swid.strip("{}") + "}"
        self.espn_s2 = espn_s2
        self.challenge = challenge
        self.write_spec = write_spec
        self._client = client or httpx.Client(
            timeout=20,
            headers={
                "accept": "application/json",
                # Identify honestly rather than impersonating a browser build.
                "user-agent": "fantasy-command-center/0.1 (personal use)",
            },
            cookies={"SWID": self.swid, "espn_s2": self.espn_s2},
        )

    # -- reads ------------------------------------------------------------
    def _get(self, path: str, **params: Any) -> dict:
        url = f"{BASE}/{path.lstrip('/')}"
        resp = self._client.get(url, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code in (401, 403):
            raise PickemError(
                "ESPN rejected the pick'em credentials (HTTP "
                f"{resp.status_code}). Refresh SWID/espn_s2 — they expire."
            )
        resp.raise_for_status()
        return resp.json()

    def challenge_info(self, week: int | None = None, view: str = "chui_default") -> dict:
        return self._get(f"challenges/{self.challenge}", scoringPeriodId=week, view=view)

    def entry(self, view: str = "chui_default") -> dict:
        return self._get(f"challenges/{self.challenge}/entries/{self.swid}", view=view)

    def propositions_raw(self, week: int | None = None) -> dict:
        return self._get("propositions", challengeId=self.challenge, scoringPeriodId=week)

    # -- parsing ----------------------------------------------------------
    def propositions(self, week: int | None = None) -> list[Proposition]:
        """Normalized list of this week's pickable games."""
        return self.parse_propositions(self.challenge_info(week=week), week=week)

    @staticmethod
    def parse_propositions(doc: dict, week: int | None = None) -> list[Proposition]:
        nodes = _first(doc, "propositions", "props", default=None)
        if nodes is None:
            # Some views nest the week's props under an events/periods array.
            for key in ("events", "scoringPeriods", "periods"):
                block = doc.get(key)
                if isinstance(block, list):
                    nodes = [p for item in block for p in item.get("propositions", [])]
                    break
        if not nodes:
            return []

        out: list[Proposition] = []
        for node in nodes:
            prop_id = str(_first(node, "id", "propositionId", default=""))
            if not prop_id:
                continue
            prop = Proposition(
                id=prop_id,
                week=_first(node, "scoringPeriodId", "week", default=week),
                lock_time=_as_dt(_first(node, "lockTime", "startTime", "date")),
                raw=node,
            )
            for opt in _first(node, "possibleOutcomes", "outcomes", "options", default=[]) or []:
                label = str(
                    _first(opt, "abbrev", "abbreviation", "name", "displayName", default="")
                )
                prop.options.append(
                    Option(
                        id=str(_first(opt, "id", "outcomeId", default="")),
                        team_code=normalize(label),
                        label=label,
                        pick_percentage=_first(opt, "percentage", "pickPercentage", "percentOwned"),
                    )
                )
            out.append(prop)
        return out

    def existing_picks(self) -> dict[str, str]:
        """Map of proposition id -> chosen option id already recorded by ESPN.

        Note ESPN's known quirk: an entry may report picks as submitted while
        withholding the selections until kickoff. An empty result therefore
        means "nothing readable", not reliably "nothing picked".
        """
        doc = self.entry()
        picks = _first(doc, "picks", default=[]) or []
        result: dict[str, str] = {}
        for p in picks:
            prop = _first(p, "propositionId", "proposition", default=None)
            opt = _first(p, "outcomeId", "selectedOutcomeId", "optionId", default=None)
            if prop is not None and opt is not None:
                result[str(prop)] = str(opt)
        return result

    # -- writes -----------------------------------------------------------
    def submit_picks(self, picks: list[Pick]) -> dict:
        """Send picks to ESPN using the captured write spec."""
        if not picks:
            return {"submitted": 0, "note": "nothing to submit"}
        if self.write_spec is None:
            raise WriteNotConfigured(
                "ESPN does not publish its pick-submission endpoint, so fcc replays a "
                "request captured from your browser. Capture it once with DevTools "
                "(Network → filter 'gambit' → make a pick → Copy as cURL) and run "
                "`fcc pickem capture-write --curl-file <file>`. "
                "Until then use --dry-run, or the Playwright fallback."
            )
        spec = self.write_spec
        body = spec.render_body(picks)
        resp = self._client.request(
            spec.method,
            spec.url.replace("{challenge}", self.challenge).replace("{swid}", self.swid),
            headers={**spec.headers, "content-type": "application/json"},
            content=body.encode(),
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"status_code": resp.status_code, "text": resp.text[:500]}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ESPNPickemClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
