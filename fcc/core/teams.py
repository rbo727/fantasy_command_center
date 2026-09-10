"""NFL team identity resolution.

This is the smallest module in the project and the one most able to lose you a
week. FantasyGuru writes prose ("we like the Ravens"), ESPN's pick'em API speaks
in proposition ids and its own abbreviations (WSH, JAC), Sleeper and nflverse use
others (WAS, JAX). Joining those without a canonical mapping is how a system
silently submits a pick for the wrong team.

The rule here is **resolve or refuse**. :func:`normalize` returns ``None`` rather
than guessing, and callers are expected to route ``None`` to human review. There
is deliberately no fuzzy matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Team:
    code: str          # canonical, nflverse-style
    city: str
    nickname: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.nickname}"


# Canonical codes follow nflverse. Aliases cover ESPN (WSH/JAC/LAR/LV), Yahoo,
# Sleeper, common prose, and recent historical names so archived content still
# resolves.
TEAMS: dict[str, Team] = {
    t.code: t
    for t in [
        Team("ARI", "Arizona", "Cardinals", ("CRD", "Phoenix Cardinals")),
        Team("ATL", "Atlanta", "Falcons", ()),
        Team("BAL", "Baltimore", "Ravens", ("RAV",)),
        Team("BUF", "Buffalo", "Bills", ()),
        Team("CAR", "Carolina", "Panthers", ()),
        Team("CHI", "Chicago", "Bears", ()),
        Team("CIN", "Cincinnati", "Bengals", ()),
        Team("CLE", "Cleveland", "Browns", ("CLV",)),
        Team("DAL", "Dallas", "Cowboys", ()),
        Team("DEN", "Denver", "Broncos", ()),
        Team("DET", "Detroit", "Lions", ()),
        Team("GB", "Green Bay", "Packers", ("GNB", "GBP")),
        Team("HOU", "Houston", "Texans", ("HTX",)),
        Team("IND", "Indianapolis", "Colts", ("CLT",)),
        Team("JAX", "Jacksonville", "Jaguars", ("JAC", "JAG", "Jags")),
        Team("KC", "Kansas City", "Chiefs", ("KAN", "KCC")),
        Team("LAC", "Los Angeles", "Chargers", ("SD", "SDG", "San Diego Chargers")),
        Team("LAR", "Los Angeles", "Rams", ("LA", "RAM", "STL", "St. Louis Rams")),
        Team("LV", "Las Vegas", "Raiders", ("OAK", "LVR", "RAI", "Oakland Raiders")),
        Team("MIA", "Miami", "Dolphins", ()),
        Team("MIN", "Minnesota", "Vikings", ("Vikes",)),
        Team("NE", "New England", "Patriots", ("NWE", "NEP", "Pats")),
        Team("NO", "New Orleans", "Saints", ("NOR", "NOS")),
        Team("NYG", "New York", "Giants", ("NGN",)),
        Team("NYJ", "New York", "Jets", ("NJN",)),
        Team("PHI", "Philadelphia", "Eagles", ()),
        Team("PIT", "Pittsburgh", "Steelers", ()),
        Team("SEA", "Seattle", "Seahawks", ("Hawks",)),
        Team("SF", "San Francisco", "49ers", ("SFO", "Niners", "49ers", "Forty Niners")),
        Team("TB", "Tampa Bay", "Buccaneers", ("TAM", "TBB", "Bucs")),
        Team("TEN", "Tennessee", "Titans", ("OTI",)),
        Team(
            "WAS",
            "Washington",
            "Commanders",
            ("WSH", "WFT", "Washington Football Team", "Redskins", "Washington Redskins"),
        ),
    ]
}

#: Strings that name a real place but not a single team. These must never
#: resolve — "New York" is two franchises and picking one is a coin flip.
AMBIGUOUS: frozenset[str] = frozenset({"ny", "newyork", "nyk"})


def _norm(text: str) -> str:
    """Fold a label to a comparison key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _build_index() -> dict[str, str]:
    index: dict[str, str] = {}

    # A bare city is a useful label ("Green Bay", "New England") but only where
    # one franchise owns it. New York and Los Angeles have two apiece, so those
    # cities are left out and fall through to the ambiguity refusal.
    city_counts: dict[str, int] = {}
    for team in TEAMS.values():
        city_counts[_norm(team.city)] = city_counts.get(_norm(team.city), 0) + 1
    unique_cities = {c for c, n in city_counts.items() if n == 1}

    def add(key: str, code: str) -> None:
        k = _norm(key)
        if not k or k in AMBIGUOUS:
            return
        # A collision would mean two teams answer to the same string, which is
        # exactly the silent-wrongness case this module exists to prevent.
        if k in index and index[k] != code:
            raise ValueError(f"alias {key!r} maps to both {index[k]} and {code}")
        index[k] = code

    for team in TEAMS.values():
        add(team.code, team.code)
        add(team.nickname, team.code)
        add(team.full_name, team.code)
        if _norm(team.city) in unique_cities:
            add(team.city, team.code)
        for alias in team.aliases:
            add(alias, team.code)
    return index


_INDEX: dict[str, str] = _build_index()


def normalize(text: str | None) -> str | None:
    """Resolve any team label to a canonical code, or ``None`` if unsure.

    ``None`` means "send this to human review" — never "pick something".

        >>> normalize("Ravens"), normalize("WSH"), normalize("New York")
        ('BAL', 'WAS', None)
    """
    if not text:
        return None
    return _INDEX.get(_norm(text))


def require(text: str | None) -> str:
    """Like :func:`normalize` but raises. Use where a miss is a bug, not input."""
    code = normalize(text)
    if code is None:
        raise ValueError(f"Could not resolve {text!r} to an NFL team")
    return code


def display(code: str) -> str:
    team = TEAMS.get(code)
    return team.full_name if team else code


_MATCHUP_SPLIT = re.compile(r"\s*(?:@|\bat\b|\bvs\.?\b|\bversus\b|-{1,2}|/)\s*", re.IGNORECASE)


def parse_matchup(text: str) -> tuple[str, str] | None:
    """Parse "BAL@KC", "Ravens at Chiefs", "KC vs BAL" into two team codes.

    Order is preserved as written; it does **not** imply home/away, since "@"
    and "vs" disagree about which side is home. Returns ``None`` unless both
    sides resolve unambiguously.
    """
    parts = [p for p in _MATCHUP_SPLIT.split(text.strip()) if p]
    if len(parts) != 2:
        return None
    a, b = normalize(parts[0]), normalize(parts[1])
    if a is None or b is None or a == b:
        return None
    return a, b


def all_codes() -> list[str]:
    return sorted(TEAMS)
