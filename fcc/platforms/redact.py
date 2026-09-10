"""Strip personal identifiers from a captured API dump while preserving shape.

The point of a dump is to show what a response *looks like* — key names, nesting,
value formats. None of that requires your account id, your entry, or the names of
everyone else in your pool.

Two rules make the output both safe and useful:

* **Shape is preserved exactly.** A UUID becomes a UUID, an int id becomes an
  int, a missing key stays missing. A redacted dump parses the same way the real
  one does, so it is a valid test fixture.
* **Pseudonyms are deterministic.** The same input value always maps to the same
  replacement, so cross-references inside the document still line up — if a user
  id appears in two places, it still matches in two places.

NFL content is deliberately kept: team names, abbreviations and matchups are the
whole reason for capturing the dump. :func:`is_nfl_content` decides that
using the same resolver the pick'em engine uses, so the two can't disagree.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from fcc.core.teams import normalize, parse_matchup

#: Keys whose values are identifiers or contact details. Always replaced.
IDENTIFIER_KEYS = (
    "swid", "guid", "uuid", "userid", "user_id", "ownerid", "owner_id",
    "entryid", "entry_id", "memberid", "member_id", "profileid", "profile_id",
    "accountid", "account_id", "email", "avatar", "imageurl", "image_url",
    "photo", "href", "link",
)

#: Id-shaped keys that name public NFL or challenge content rather than a
#: person. These are the values a fixture actually needs, so they survive.
#: Everything else id-shaped is redacted by default — see :func:`_should_redact`.
GLOBAL_ID_KEYS = frozenset({
    "id", "propositionid", "proposition_id", "outcomeid", "outcome_id",
    "challengeid", "challenge_id", "scoringperiodid", "scoring_period_id",
    "eventid", "event_id", "gameid", "game_id", "competitionid", "competition_id",
    "teamid", "team_id", "athleteid", "athlete_id", "seasonid", "season_id",
    "periodid", "period_id", "weekid", "week_id",
})

#: Path segments that mark a subtree describing a person or a private group.
#: Any id inside one of these is personal, including a bare "id".
PERSONAL_SUBTREES = (
    "group", "member", "user", "owner", "entry", "profile", "account",
    "leaderboard", "friend", "opponent",
)

#: Keys that may hold a person's name. Kept only if the value is NFL content.
NAME_KEYS = (
    "name", "displayname", "display_name", "nickname", "username", "handle",
    "alias", "firstname", "lastname", "fullname", "title", "label",
    "teamname", "team_name", "entryname", "entry_name",
)

#: Value shapes that are identifiers wherever they appear, whatever the key.
UUID_RE = re.compile(
    r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$"
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _digest(value: Any, length: int = 8) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:length]


def is_nfl_content(value: str) -> bool:
    """True if this string is a team, an abbreviation, or a matchup.

    Uses the same resolver as the pick'em engine, so redaction and pick
    matching can never disagree about what counts as a team.
    """
    return normalize(value) is not None or parse_matchup(value) is not None


@dataclass
class RedactionReport:
    """What was removed, by key path — never the values themselves.

    Deliberately records paths and counts only. The report travels inside the
    shareable file, so putting originals in it would undo the redaction.
    """

    paths: dict[str, int] = field(default_factory=dict)
    kept_names: list[str] = field(default_factory=list)

    def record(self, path: str) -> None:
        key = re.sub(r"\[\d+\]", "[]", path)  # collapse list indices
        self.paths[key] = self.paths.get(key, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.paths.values())

    def as_dict(self) -> dict:
        return {
            "note": (
                "Personal identifiers were replaced with deterministic "
                "pseudonyms. Structure, types and NFL content are unchanged."
            ),
            "redacted_value_count": self.total,
            "redacted_paths": dict(sorted(self.paths.items())),
        }


def _pseudonym(value: Any, kind: str) -> Any:
    """Return a replacement with the same shape as *value*."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        # Keep it an int, and keep it stable.
        return int(_digest(value, 8), 16) % 10_000_000
    text = str(value)
    if UUID_RE.match(text):
        h = _digest(text, 32)
        uuid_like = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        return "{" + uuid_like + "}" if text.startswith("{") else uuid_like
    if EMAIL_RE.match(text):
        return f"user-{_digest(text)}@example.invalid"
    if text.startswith(("http://", "https://")):
        return f"https://redacted.invalid/{_digest(text)}"
    return f"{kind}-{_digest(text)}"


def _in_personal_subtree(path: str) -> bool:
    """True if the key's *immediate* parent object describes a person or group.

    Only the immediate parent counts. Matching any ancestor looks safer but
    isn't: a dump wrapped under a key like `entry` would mark the entire
    document personal and strip the proposition and outcome ids the fixture
    exists to show. The nearest enclosing object is what actually says whose
    data this is — `group.members[].id` is personal, `entry.propositions[].id`
    is not.
    """
    segments = [seg.replace("[]", "") for seg in path.lower().split(".") if seg]
    if len(segments) < 2:
        return False
    parent = re.sub(r"\[\d+\]", "", segments[-2])
    return any(seg in parent for seg in PERSONAL_SUBTREES)


def _should_redact(key: str, value: Any, path: str = "") -> str | None:
    """Return the pseudonym kind if this key/value should be replaced.

    Identifiers are handled default-deny. An enumerated list of "sensitive key
    names" can only ever be as complete as the last response you looked at —
    `groupId` slipped through exactly that way — so anything id-shaped is
    redacted unless it is a known-public NFL or challenge id, and even those go
    when they sit inside a personal subtree (a group's `id` is the group's).
    Over-redaction costs a little fixture detail; under-redaction leaks.
    """
    k = key.lower()

    if any(pat in k for pat in IDENTIFIER_KEYS):
        return "id"

    if k.endswith("id") or k.endswith("_id") or k == "id":
        if _in_personal_subtree(path):
            return "id"
        if k not in GLOBAL_ID_KEYS:
            return "id"
        return None

    if isinstance(value, str):
        if UUID_RE.match(value) or EMAIL_RE.match(value):
            return "id"
        if any(pat == k or pat in k for pat in NAME_KEYS):
            # Team names and matchups are the payload we came for; a name that
            # isn't NFL content is a person's, and goes.
            return None if is_nfl_content(value) else "name"
    return None


def redact(node: Any, report: RedactionReport | None = None, path: str = "") -> Any:
    """Recursively redact a parsed JSON document."""
    report = report if report is not None else RedactionReport()

    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            kind = _should_redact(key, value, child)
            if kind and not isinstance(value, dict | list):
                out[key] = _pseudonym(value, kind)
                report.record(child)
            else:
                if (
                    isinstance(value, str)
                    and any(p == key.lower() or p in key.lower() for p in NAME_KEYS)
                    and is_nfl_content(value)
                ):
                    report.kept_names.append(value)
                out[key] = redact(value, report, child)
        return out

    if isinstance(node, list):
        return [redact(item, report, f"{path}[{i}]") for i, item in enumerate(node)]

    return node


def redact_document(doc: Any) -> tuple[Any, RedactionReport]:
    """Redact a document and attach a summary of what was removed."""
    report = RedactionReport()
    result = redact(doc, report)
    if isinstance(result, dict):
        result["_fcc_redaction"] = report.as_dict()
    return result, report
