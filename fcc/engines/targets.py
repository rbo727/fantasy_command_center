"""Who to add — the discovery half of waiver work.

`faab bid` prices a player you already named. This answers the question you
actually have on a Tuesday: *given my roster and this week's holes, who on the
wire is worth bidding on at all?*

The distinction that saves money here is **how long the hole lasts**:

* A **bye** is a one-week hole. Whoever you add is a rental, and paying a
  multi-week price for one start is the classic way a budget bleeds out.
* An **injured starter** out several weeks is a multi-week hole, and the add
  keeps paying every week he's out.
* A **season-ending** injury means you are buying the rest of the year.

The same player is worth very different money in those three cases, so targets
carry their need's duration and the recommended bid is scaled by it. Everything
here is advisory: FAAB is money tier, so a human approves every claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fcc.engines.value import RankedPlayer, value_over_available, value_over_replacement
from fcc.platforms.base import PlayerStatus, Roster, RosterSlot

log = logging.getLogger(__name__)

#: Slots that are not part of the active lineup.
NON_STARTING = {"BN", "BE", "IR", "TAXI", "RES"}

#: How many weeks a hole is assumed to last, by what caused it. A bye is exactly
#: one week; an injury is open-ended until news says otherwise, and the
#: conservative assumption is the shorter one, because overpaying is the error
#: that compounds.
NEED_WEEKS = {
    "bye": 1,
    "out": 2,
    "doubtful": 1,
    "ir": 6,
    "pup": 6,
    "suspended": 4,
    "inactive": 1,
    "empty": 8,
    "unknown": 1,
}


@dataclass
class Need:
    """One hole in the starting lineup."""

    slot: str
    position: str
    cause: str
    incumbent: str = ""
    weeks: int = 1

    @property
    def is_rental(self) -> bool:
        """A one-week hole. Rent, don't buy."""
        return self.weeks <= 1


@dataclass
class Target:
    """An available player worth considering, and why."""

    player: RankedPlayer
    vor: float | None = None
    fills: Need | None = None
    beats_current: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Ranking score. Need-weighted value, not raw projection."""
        base = self.vor if self.vor is not None else 0.0
        if self.fills is None:
            return base * 0.5          # useful, but nothing is broken
        # A multi-week hole is worth more than a one-week rental.
        return base * (1.4 if not self.fills.is_rental else 1.0)


def _cause_for(slot: RosterSlot, week: int | None) -> str | None:
    """Why this slot is a hole, or None if it isn't."""
    if slot.player is None:
        return "empty"
    player = slot.player
    if player.bye_week and week and player.bye_week == week:
        return "bye"
    if player.status.is_unavailable:
        return player.status.value
    if player.status is PlayerStatus.DOUBTFUL:
        return "doubtful"
    return None


def roster_needs(roster: Roster, week: int | None = None) -> list[Need]:
    """Every starting slot that currently has nobody usable in it."""
    week = week if week is not None else roster.week
    needs: list[Need] = []
    for slot in roster.slots:
        if not slot.starter or slot.slot.upper() in NON_STARTING:
            continue
        cause = _cause_for(slot, week)
        if cause is None:
            continue
        position = (slot.player.position if slot.player else slot.slot) or slot.slot
        needs.append(
            Need(
                slot=slot.slot,
                position=position.upper(),
                cause=cause,
                incumbent=slot.player.name if slot.player else "",
                weeks=NEED_WEEKS.get(cause, 1),
            )
        )
    return needs


def my_best_at(roster: Roster, position: str, week: int | None = None) -> float | None:
    """Best projection I already have at a position, among players who can play."""
    week = week if week is not None else roster.week
    best: float | None = None
    for slot in roster.slots:
        player = slot.player
        if player is None or (player.position or "").upper() != position.upper():
            continue
        if player.status.is_unavailable:
            continue
        if player.bye_week and week and player.bye_week == week:
            continue
        if player.projected_points is None:
            continue
        best = player.projected_points if best is None else max(best, player.projected_points)
    return best


def find_targets(
    available: list[RankedPlayer],
    universe: list[RankedPlayer],
    roster: Roster,
    teams: int,
    week: int | None = None,
    positions: list[str] | None = None,
    limit: int = 15,
) -> tuple[list[Target], list[Need]]:
    """Rank the wire against this roster's actual holes.

    ``universe`` is every projected player, rostered or not — replacement level
    is defined relative to the whole league, so scoring free agents against a
    free-agent-only pool would quietly understate every gap.
    """
    week = week if week is not None else roster.week
    needs = roster_needs(roster, week)
    needs_by_position: dict[str, Need] = {}
    for need in needs:
        # Keep the longest-lasting need per position: a season-ending injury
        # outranks a bye when deciding what the position is worth.
        existing = needs_by_position.get(need.position)
        if existing is None or need.weeks > existing.weeks:
            needs_by_position[need.position] = need

    wanted = {p.upper() for p in positions} if positions else None
    targets: list[Target] = []

    for player in available:
        if player.projected_points is None:
            continue
        if wanted and player.pos not in wanted:
            continue

        # Prefer the measured gap to the next free alternative over the
        # rank-based estimate: when the rostered set is known, looking beats
        # assuming. Fall back to the estimate only if the pool is unusable.
        vor = value_over_available(player, available)
        if vor is None:
            vor = value_over_replacement(player, universe, teams)
        if vor is None or vor <= 0:
            continue

        need = needs_by_position.get(player.pos)
        current = my_best_at(roster, player.pos, week)
        beats = (
            player.projected_points - current
            if current is not None and player.projected_points is not None
            else None
        )

        reasons = [
            f"{player.projected_points:.1f} proj, {vor:+.1f} over the next free "
            f"{player.pos}"
        ]
        if need:
            horizon = "this week only" if need.is_rental else f"~{need.weeks} weeks"
            reasons.append(
                f"fills {need.slot} ({need.incumbent or 'empty'} {need.cause}) — {horizon}"
            )
        if beats is not None:
            reasons.append(
                f"{'+' if beats >= 0 else ''}{beats:.1f} vs your best available {player.pos}"
            )
        elif current is None:
            reasons.append(f"you have no usable {player.pos} projected this week")

        targets.append(
            Target(player=player, vor=vor, fills=need, beats_current=beats, reasons=reasons)
        )

    targets.sort(key=lambda t: (-t.score, t.player.name))
    return targets[:limit], needs


def rental_warning(target: Target, bid: int, budget_remaining: int) -> str | None:
    """Flag paying a multi-week price for a one-week rental.

    The single most common way a FAAB budget bleeds out is a series of
    reasonable-looking bids on bye-week patches.
    """
    if target.fills is None or not target.fills.is_rental:
        return None
    if not budget_remaining:
        return None
    share = bid / budget_remaining
    if share >= 0.10:
        return (
            f"${bid} is {share:.0%} of your remaining budget for a one-week bye patch. "
            "Rentals are where budgets bleed out — consider the cheapest body that "
            "fills the slot."
        )
    return None
