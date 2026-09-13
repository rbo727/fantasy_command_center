"""The lineup guardian — never start a player who cannot play.

The job is narrow on purpose. It finds starters who are *definitely* out and
swaps in a bench player who is *definitely* eligible. Everything else — a
Questionable starter, a slot it can't fill, a slot type it doesn't recognise —
becomes a warning for you rather than an automatic decision.

That asymmetry is the whole design. An unnecessary warning costs you ten
seconds; an automatic swap made on a wrong assumption costs a week. So:

* Only ``PlayerStatus.is_unavailable`` triggers a swap. Questionable never does.
* A slot type not in :data:`SLOT_ELIGIBILITY` fills nothing — an unrecognised
  slot means we don't know the rules, and guessing them produces illegal
  lineups that look fine until they're rejected.
* A bench player is used at most once per plan, even when several starters are
  out.
* Byes are treated as unavailability regardless of injury status, because a
  healthy player on bye still scores zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fcc.platforms.base import Player, PlayerStatus, Roster, RosterSlot

log = logging.getLogger(__name__)

#: Which positions may fill which slot. Keys are upper-cased slot names as the
#: platforms spell them. A slot missing from this map is never auto-filled.
SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF", "DST"},
    "DST": {"DEF", "DST"},
    # Flex variants, by every spelling the platforms use.
    "FLEX": {"RB", "WR", "TE"},
    "W/R/T": {"RB", "WR", "TE"},
    "WRT": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "W/R": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "WRTE_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "SUPERFLEX": {"QB", "RB", "WR", "TE"},
    "SFLEX": {"QB", "RB", "WR", "TE"},
    "OP": {"QB", "RB", "WR", "TE"},
    "Q/W/R/T": {"QB", "RB", "WR", "TE"},
    # Individual defensive players.
    "DL": {"DL", "DE", "DT"},
    "LB": {"LB"},
    "DB": {"DB", "CB", "S"},
    "IDP_FLEX": {"DL", "DE", "DT", "LB", "DB", "CB", "S"},
}

#: Slots that are never part of the active lineup.
NON_STARTING = {"BN", "BE", "IR", "TAXI", "RES"}


@dataclass
class Swap:
    """One proposed lineup change."""

    slot: str
    slot_index: int
    out_player: Player
    in_player: Player
    reason: str

    def describe(self) -> str:
        return (
            f"{self.slot}: {self.out_player.name} "
            f"({self.out_player.status.value}) -> {self.in_player.name}"
        )


@dataclass
class LineupWarning:
    """Something a human should look at. Never acted on automatically."""

    kind: str
    detail: str
    slot: str = ""
    player: str = ""


@dataclass
class LineupPlan:
    swaps: list[Swap] = field(default_factory=list)
    warnings: list[LineupWarning] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.swaps and not self.warnings

    def summary(self) -> str:
        return f"{len(self.swaps)} swap(s), {len(self.warnings)} warning(s)"


def eligible_positions(slot: str) -> set[str] | None:
    """Positions allowed in *slot*, or ``None`` if the slot type is unknown.

    ``None`` means "we don't know the rules here" and must be treated as
    "don't touch it" — never as "anything goes".
    """
    return SLOT_ELIGIBILITY.get(slot.upper().strip())


def is_unavailable(player: Player, week: int | None = None) -> bool:
    """Whether this player definitely cannot produce points this week."""
    if player.status.is_unavailable:
        return True
    # A healthy player on bye still scores zero.
    return bool(week and player.bye_week and player.bye_week == week)


def unavailability_reason(player: Player, week: int | None = None) -> str:
    if player.status.is_unavailable:
        return player.status.value
    if week and player.bye_week == week:
        return "bye"
    return ""


def candidates_for(
    slot: str,
    bench: list[RosterSlot],
    week: int | None = None,
    taken: set[str] | None = None,
) -> list[Player]:
    """Bench players who could legally start in *slot*, best first.

    Ordered by projected points where the platform gives them, then by name so
    the result is deterministic — a guardian that proposes a different swap on
    every run is impossible to trust or test.
    """
    allowed = eligible_positions(slot)
    if allowed is None:
        return []
    taken = taken or set()

    usable = [
        s.player
        for s in bench
        if s.player
        and s.player.player_id not in taken
        and not is_unavailable(s.player, week)
        and s.player.eligible_at(allowed)
    ]
    return sorted(
        usable,
        key=lambda p: (-(p.projected_points if p.projected_points is not None else -1), p.name),
    )


def plan_lineup(roster: Roster, week: int | None = None) -> LineupPlan:
    """Work out which starters must come out, and who replaces them."""
    week = week if week is not None else roster.week
    plan = LineupPlan()
    bench = [s for s in roster.slots if not s.starter and s.slot.upper() not in {"IR", "TAXI"}]
    taken: set[str] = set()

    for index, slot in enumerate(roster.slots):
        if not slot.starter:
            continue

        if slot.player is None:
            plan.warnings.append(
                LineupWarning("empty_slot", f"{slot.slot} has nobody in it", slot=slot.slot)
            )
            continue

        player = slot.player

        # Unknown slot types are left alone entirely — including their warnings,
        # which say why rather than pretending the lineup is fine.
        if eligible_positions(slot.slot) is None:
            if is_unavailable(player, week):
                plan.warnings.append(
                    LineupWarning(
                        "unknown_slot",
                        f"{player.name} is {unavailability_reason(player, week)} but ffm does "
                        f"not know what may fill a {slot.slot!r} slot, so it will not guess",
                        slot=slot.slot,
                        player=player.name,
                    )
                )
            continue

        if is_unavailable(player, week):
            reason = unavailability_reason(player, week)
            options = candidates_for(slot.slot, bench, week, taken)
            if not options:
                plan.warnings.append(
                    LineupWarning(
                        "no_replacement",
                        f"{player.name} is {reason} and no eligible, available bench player "
                        f"can fill {slot.slot}",
                        slot=slot.slot,
                        player=player.name,
                    )
                )
                continue
            replacement = options[0]
            taken.add(replacement.player_id)
            plan.swaps.append(
                Swap(
                    slot=slot.slot,
                    slot_index=index,
                    out_player=player,
                    in_player=replacement,
                    reason=f"{player.name} is {reason}",
                )
            )
            continue

        # Playable but not certain. Flagged, never swapped: benching a
        # Questionable starter who then plays is its own kind of loss.
        if player.status.is_doubtful:
            plan.warnings.append(
                LineupWarning(
                    "questionable",
                    f"{player.name} is {player.status.value} — check before kickoff",
                    slot=slot.slot,
                    player=player.name,
                )
            )

    return plan


def apply_plan(roster: Roster, plan: LineupPlan) -> Roster:
    """Return what the roster would look like after the swaps.

    Used for previews and for the post-write verification read-back; it never
    mutates the roster it is given.
    """
    slots = [RosterSlot(slot=s.slot, player=s.player, starter=s.starter) for s in roster.slots]
    bench_index = {
        s.player.player_id: i for i, s in enumerate(slots) if s.player and not s.starter
    }

    for swap in plan.swaps:
        target = slots[swap.slot_index]
        source_index = bench_index.get(swap.in_player.player_id)
        if source_index is None:
            log.warning("replacement %s is not on the bench; skipping", swap.in_player.name)
            continue
        slots[source_index] = RosterSlot(
            slot=slots[source_index].slot, player=target.player, starter=False
        )
        slots[swap.slot_index] = RosterSlot(
            slot=target.slot, player=swap.in_player, starter=True
        )

    return Roster(
        league_key=roster.league_key,
        team_id=roster.team_id,
        team_name=roster.team_name,
        week=roster.week,
        slots=slots,
    )


def verify_plan_applied(roster_after: Roster, plan: LineupPlan) -> tuple[bool, list[str]]:
    """Check a freshly-read roster actually reflects the swaps.

    This is what the action gate's verifier calls. A lineup change that the
    platform silently dropped is exactly the failure the gate exists to catch.
    """
    problems: list[str] = []
    starters = {s.player.player_id for s in roster_after.starters if s.player}

    for swap in plan.swaps:
        if swap.in_player.player_id not in starters:
            problems.append(f"{swap.in_player.name} is not starting")
        if swap.out_player.player_id in starters:
            problems.append(f"{swap.out_player.name} is still starting")

    # Belt and braces: whatever else happened, nobody unavailable may remain in
    # the lineup.
    for slot in roster_after.starters:
        if slot.player and is_unavailable(slot.player, roster_after.week):
            problems.append(
                f"{slot.player.name} ({slot.player.status.value}) is still in {slot.slot}"
            )

    return not problems, problems


def unavailable_starters(roster: Roster, week: int | None = None) -> list[RosterSlot]:
    """Every started player who cannot play. The dashboard's red rows."""
    week = week if week is not None else roster.week
    return [s for s in roster.starters if s.player and is_unavailable(s.player, week)]


__all__ = [
    "SLOT_ELIGIBILITY",
    "LineupPlan",
    "LineupWarning",
    "PlayerStatus",
    "Swap",
    "apply_plan",
    "candidates_for",
    "eligible_positions",
    "is_unavailable",
    "plan_lineup",
    "unavailable_starters",
    "verify_plan_applied",
]
