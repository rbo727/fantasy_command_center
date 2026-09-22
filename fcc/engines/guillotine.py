"""Guillotine-league bidding.

A guillotine league is a different game from redraft, and a FAAB model tuned for
redraft gives actively bad advice in it:

* **You are eliminated for being worst, not for failing to be best.** Your
  weekly *floor* matters more than your ceiling, which changes which players are
  worth paying for.
* **The pool arrives in lumps.** Every week a whole chopped roster hits waivers,
  full of drafted players — so preseason value stays relevant in a way it never
  is on a normal waiver wire.
* **Budget is finite and never replenishes,** while the field shrinks each week.
  A dollar's competitive worth rises as rivals are eliminated and their budgets
  leave the pool with them.

The single most decision-relevant fact here is not a projection — it is
:attr:`GuillotineState.max_rival_budget`. If you hold more FAAB than any
surviving rival, you cannot be outbid, and the only remaining question is what
the player is worth to you. Most guillotine bidding advice is a proxy for
working that out; here it is computed directly.

Pacing anchors follow widely-published guillotine strategy: heavy early
restraint (a single early buy above ~10-15% of budget is the classic way to
lose), loosening as the field thins, because unspent FAAB in a league you have
been chopped out of is worth exactly nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Share of *remaining* budget it is sane to commit to one player, by how far
#: the field has thinned. 0.0 = opening week, 1.0 = heads-up.
PACING_CURVE: list[tuple[float, float]] = [
    (0.00, 0.12),
    (0.25, 0.20),
    (0.50, 0.35),
    (0.75, 0.55),
    (1.00, 0.85),
]


@dataclass
class GuillotineState:
    """Who is still alive, and what they can still spend."""

    my_remaining: int
    rival_remaining: list[int] = field(default_factory=list)
    teams_alive: int | None = None
    teams_at_start: int | None = None
    week: int | None = None

    def __post_init__(self) -> None:
        if self.teams_alive is None:
            self.teams_alive = len(self.rival_remaining) + 1

    # -- the facts that decide a bid --------------------------------------
    @property
    def max_rival_budget(self) -> int:
        """The most any single surviving rival could bid."""
        return max(self.rival_remaining) if self.rival_remaining else 0

    @property
    def field_remaining(self) -> int:
        return sum(self.rival_remaining)

    @property
    def my_share(self) -> float | None:
        """Your slice of all FAAB still live in the league."""
        total = self.my_remaining + self.field_remaining
        return self.my_remaining / total if total else None

    @property
    def can_outbid_anyone(self) -> bool:
        """True when no rival can match you, whatever they do."""
        return self.my_remaining > self.max_rival_budget

    @property
    def price_to_guarantee(self) -> int | None:
        """The bid nobody alive can beat, if you can afford it.

        ``None`` when even your whole budget cannot clear the richest rival —
        in which case winning is not something you can buy, only hope for.
        """
        needed = self.max_rival_budget + 1
        return needed if needed <= self.my_remaining else None

    # -- pacing -----------------------------------------------------------
    @property
    def progress(self) -> float:
        """0.0 at the opening week, 1.0 heads-up."""
        start = self.teams_at_start or self.teams_alive
        if not start or start <= 1 or self.teams_alive is None:
            return 0.0
        return max(0.0, min(1.0, (start - self.teams_alive) / (start - 1)))

    def pacing_fraction(self) -> float:
        """Share of remaining budget it is sane to commit to one player now."""
        progress = self.progress
        if progress <= PACING_CURVE[0][0]:
            return PACING_CURVE[0][1]
        if progress >= PACING_CURVE[-1][0]:
            return PACING_CURVE[-1][1]
        for (x0, y0), (x1, y1) in zip(PACING_CURVE, PACING_CURVE[1:], strict=False):
            if x0 <= progress <= x1:
                span = x1 - x0
                return y0 if span == 0 else y0 + (y1 - y0) * (progress - x0) / span
        return PACING_CURVE[-1][1]

    def spend_cap(self) -> int:
        """Pacing ceiling for a single player, in dollars."""
        return max(1, round(self.my_remaining * self.pacing_fraction()))

    # -- reporting --------------------------------------------------------
    def advice(self) -> list[str]:
        """Plain statements a human can act on, strongest first."""
        out: list[str] = []
        if self.can_outbid_anyone:
            guarantee = self.price_to_guarantee
            out.append(
                f"You hold more FAAB (${self.my_remaining}) than any surviving rival "
                f"(richest has ${self.max_rival_budget}). ${guarantee} wins any player "
                f"outright - nobody alive can match it."
            )
        else:
            out.append(
                f"At least one rival (${self.max_rival_budget}) can outbid your "
                f"${self.my_remaining}. You cannot buy a guaranteed win."
            )
        if self.my_share is not None:
            out.append(
                f"You control {self.my_share:.0%} of the FAAB still live in the league "
                f"across {self.teams_alive} surviving teams."
            )
        out.append(
            f"Pacing at this stage suggests keeping a single buy under "
            f"${self.spend_cap()} ({self.pacing_fraction():.0%} of what you hold). "
            f"Exceed it deliberately, for a player who changes your weekly floor - "
            f"not to win a coin flip."
        )
        return out


def from_budget_rows(
    rows: list[dict], teams_at_start: int | None = None, week: int | None = None
) -> GuillotineState:
    """Build state from :meth:`SleeperClient.league_budgets` output.

    Rosters inferred as chopped are excluded from the live field: their budget
    cannot be bid against you.
    """
    mine = next((r for r in rows if r.get("is_me")), None)
    if mine is None or mine.get("budget_remaining") is None:
        raise ValueError(
            "Could not identify your roster or its remaining budget. Check team_id "
            "in config/leagues.yml."
        )

    alive_rivals = [
        r["budget_remaining"]
        for r in rows
        if not r.get("is_me")
        and not r.get("likely_chopped")
        and r.get("budget_remaining") is not None
    ]
    return GuillotineState(
        my_remaining=int(mine["budget_remaining"]),
        rival_remaining=[int(v) for v in alive_rivals],
        teams_alive=len(alive_rivals) + 1,
        teams_at_start=teams_at_start or len(rows),
        week=week,
    )
