"""Value over replacement — what a player is worth to *this* roster.

A projection alone doesn't price a player. 12 points a week is a fine TE and a
bad RB, because you can get a 10-point RB for free and the best free TE scores
4. What matters is the gap to whatever you could have for nothing, and that gap
depends on the league's shape: how many teams start how many of that position.

Replacement level here is the best player you could still get for free — the
(teams x starters + 1)-th ranked player at the position, since the ones above
that are already rostered somewhere.

**In a guillotine league this moves every week.** The field shrinks, so fewer
starting slots exist league-wide and replacement level *rises* — the same
projection is worth less in a four-team endgame than in a twelve-team week 1.
:func:`replacement_rank` takes the live team count for that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Typical starters per team by position, for a standard lineup. Flex demand is
#: spread across RB/WR/TE rather than modelled as its own slot, which is why
#: these carry fractions.
DEFAULT_STARTERS: dict[str, float] = {
    "QB": 1.0,
    "RB": 2.5,   # 2 + a share of flex
    "WR": 2.8,   # 3 + a share of flex, minus flex often going to RB
    "TE": 1.2,
    "K": 1.0,
    "DEF": 1.0,
}


@dataclass
class RankedPlayer:
    """One player from a rankings source."""

    name: str
    position: str
    projected_points: float | None = None
    positional_rank: int | None = None
    overall_rank: int | None = None
    #: Platform id, when the source is one (Sleeper) rather than free text
    #: extracted from a page. Lets callers match a player without a name
    #: lookup, which is the only reliable way once suffixes/spelling enter it.
    player_id: str | None = None
    team: str | None = None

    @property
    def pos(self) -> str:
        return (self.position or "").upper()


def replacement_rank(position: str, teams: int, starters: dict[str, float] | None = None) -> int:
    """Positional rank of the best freely-available player.

    Everyone ranked above this is rostered somewhere, so this is the bar a
    waiver add has to clear to be worth anything at all.
    """
    starters = starters or DEFAULT_STARTERS
    per_team = starters.get(position.upper(), 1.0)
    return max(1, int(round(teams * per_team)) + 1)


def replacement_points(
    players: list[RankedPlayer],
    position: str,
    teams: int,
    starters: dict[str, float] | None = None,
) -> float | None:
    """Projected points of the replacement-level player at *position*.

    ``None`` when the rankings don't go deep enough to see replacement level —
    an honest "I can't tell" rather than a number derived from the last player
    who happens to be in the list.
    """
    pool = sorted(
        (p for p in players if p.pos == position.upper() and p.projected_points is not None),
        key=lambda p: -p.projected_points,
    )
    if not pool:
        return None

    index = replacement_rank(position, teams, starters) - 1
    if index >= len(pool):
        log.info(
            "rankings only go %d deep at %s; replacement level (%d) is past the end",
            len(pool), position, index + 1,
        )
        return None
    return pool[index].projected_points


def value_over_replacement(
    player: RankedPlayer,
    players: list[RankedPlayer],
    teams: int,
    starters: dict[str, float] | None = None,
) -> float | None:
    """How many points a week this player adds over a free alternative.

    ``None`` when either the player or replacement level is unknown — the FAAB
    model treats that as "no recommendation" rather than assuming zero.
    """
    if player.projected_points is None:
        return None
    baseline = replacement_points(players, player.pos, teams, starters)
    if baseline is None:
        return None
    return player.projected_points - baseline


def upgrade_over(
    player: RankedPlayer, incumbent: RankedPlayer | None
) -> float | None:
    """Points gained over the player he'd actually replace in your lineup.

    Usually the more honest number than raw VOR: if your current starter is
    already good, a good addition adds little. With the incumbent injured,
    pass ``None`` and use :func:`value_over_replacement` instead — that is
    exactly the case where replacement level is the right comparison.
    """
    if player.projected_points is None:
        return None
    if incumbent is None or incumbent.projected_points is None:
        return None
    return player.projected_points - incumbent.projected_points


def top_free_agents_by_position(
    players: list[RankedPlayer],
    rostered_ids: set[str],
    limit: int = 10,
) -> dict[str, list[RankedPlayer]]:
    """Free agents only, ranked by projection, grouped by position.

    A player with no projection is left out rather than sorted to the bottom
    as a 0 - unprojected is "unknown", not "worthless", and showing it as 0
    would misrepresent a thin position's honest low numbers.
    """
    out: dict[str, list[RankedPlayer]] = {}
    for p in players:
        if p.player_id in rostered_ids or p.projected_points is None:
            continue
        out.setdefault(p.pos, []).append(p)
    for pos, group in out.items():
        group.sort(key=lambda p: -p.projected_points)
        out[pos] = group[:limit]
    return out


def find(players: list[RankedPlayer], name: str) -> RankedPlayer | None:
    """Exact-then-substring name lookup, refusing ambiguity."""
    needle = name.strip().lower()
    exact = [p for p in players if p.name.strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    partial = [p for p in players if needle in p.name.lower()]
    return partial[0] if len(partial) == 1 else None
