"""Turn staff picks into ESPN pick'em submissions.

The hard part is not fetching or sending — it's the join. FantasyGuru writes
about *betting markets*; ESPN pick'em asks a different question. Three specific
traps this module exists to handle:

1. **Market mismatch.** Staff picks are frequently against the spread. An ATS
   pick on a +7 dog is a bet the dog stays close, which is usually a
   straight-up *loss* pick. Passing the leaned side through as the winner would
   quietly tank a pick'em entry. See :func:`straight_up_side`.
2. **Identity mismatch.** Prose names ("the Ravens") must resolve to the exact
   option id inside an ESPN proposition. Handled by :mod:`ffm.core.teams`,
   which refuses rather than guesses.
3. **Silent partial coverage.** A week where only 11 of 16 games matched should
   not look like a successful run. Everything unmatched lands in
   :attr:`JoinResult.review` and gets surfaced.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Literal

from ffm.core.teams import normalize
from ffm.platforms.espn_pickem import Pick, Proposition

log = logging.getLogger(__name__)

Market = Literal["ATS", "ML", "SU", "OU", "UNKNOWN"]

#: Standard deviation of NFL game margin around the closing spread, in points.
#: ~13.5 is the long-standing empirical value; it makes a 3-point favorite a
#: ~59% winner and a 7-point favorite ~70%, which matches observed rates.
MARGIN_SIGMA = 13.5

#: How much a staff lean moves our win-probability estimate, per conviction
#: point (1-5). Deliberately small: their edge is against the spread, and a
#: strong ATS opinion is not a claim about who wins outright.
LEAN_WEIGHT = 0.01

#: Below this edge over a coin flip, a straight-up pick isn't really a pick.
#: Such games are still submitted (you must pick someone) but are flagged and
#: given the lowest confidence points.
COINFLIP_EDGE = 0.03


@dataclass
class StaffPick:
    """One recommendation as parsed out of FantasyGuru's page."""

    matchup: str
    team: str
    market: Market = "UNKNOWN"
    spread: float | None = None
    conviction: int = 3
    rationale: str = ""

    @property
    def team_code(self) -> str | None:
        return normalize(self.team)


@dataclass
class ReviewItem:
    """Something we refused to decide automatically."""

    reason: str
    detail: str
    staff_pick: StaffPick | None = None
    proposition: Proposition | None = None


@dataclass
class JoinResult:
    picks: list[Pick] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    #: proposition id -> our estimated win probability for the picked side
    confidence_by_prop: dict[str, float] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.review

    def summary(self) -> str:
        return f"{len(self.picks)} picks, {len(self.review)} needing review"


def win_probability(spread: float) -> float:
    """Probability a team wins outright given *its own* point spread.

    ``spread`` uses betting convention: negative means favored. A team laying
    3 points has ``spread=-3``.

        >>> round(win_probability(-7), 3)
        0.698
        >>> win_probability(0)
        0.5
    """
    expected_margin = -spread
    # Normal CDF via erf; avoids a scipy dependency for one function.
    return 0.5 * (1 + math.erf(expected_margin / (MARGIN_SIGMA * math.sqrt(2))))


def straight_up_side(pick: StaffPick) -> tuple[str | None, float, str]:
    """Translate a staff pick into a straight-up side, with a probability.

    Returns ``(team_code, probability, explanation)``. ``team_code`` is ``None``
    when the pick can't be translated and needs human review.

    The key case: an ATS lean on an underdog. We take the lean as mild evidence
    for that side, then still pick whoever is more likely to win the game. This
    is why an ATS-driven pick'em entry does not simply mirror the staff's card.
    """
    leaned = pick.team_code
    if leaned is None:
        return None, 0.0, f"could not resolve team {pick.team!r}"

    # Moneyline and straight-up picks are already answering our question.
    if pick.market in ("ML", "SU"):
        prob = (
            win_probability(pick.spread)
            if pick.spread is not None
            else 0.5 + LEAN_WEIGHT * pick.conviction
        )
        return leaned, prob, f"{pick.market} pick taken directly"

    if pick.market == "OU":
        return None, 0.0, "total (over/under) pick says nothing about a winner"

    if pick.spread is None:
        # An ATS lean with no spread attached is unusable for straight-up:
        # we can't tell whether the side is favored.
        if pick.market == "ATS":
            return None, 0.0, "ATS pick with no spread — cannot infer a winner"
        # Unknown market, no spread: treat as a weak straight-up lean.
        return leaned, 0.5 + LEAN_WEIGHT * pick.conviction, "unknown market, treated as weak lean"

    base = win_probability(pick.spread)
    adjusted = min(0.99, base + LEAN_WEIGHT * pick.conviction)

    if adjusted >= 0.5:
        return leaned, adjusted, f"leaned side is favored ({pick.spread:+.1f}); taking it"

    # The staff like a dog that is still more likely to lose outright.
    opponent_prob = 1 - adjusted
    return (
        None,
        opponent_prob,
        f"ATS lean on a {pick.spread:+.1f} underdog — straight-up winner is the other side",
    )


def _match_proposition(props: list[Proposition], team_code: str) -> Proposition | None:
    hits = [p for p in props if team_code in p.team_codes]
    if len(hits) == 1:
        return hits[0]
    # A team playing twice in one week's slate means our week filter is wrong;
    # refuse rather than pick arbitrarily.
    if len(hits) > 1:
        log.warning("team %s appears in %d propositions; refusing", team_code, len(hits))
    return None


def join(
    staff_picks: list[StaffPick],
    propositions: list[Proposition],
    use_confidence_points: bool = False,
) -> JoinResult:
    """Match staff picks to ESPN propositions and produce submittable picks."""
    result = JoinResult()
    seen_props: set[str] = set()

    for sp in staff_picks:
        code, prob, why = straight_up_side(sp)

        if code is None and "other side" in why:
            # Translatable, just to the opposite team. Find the game first.
            leaned = sp.team_code
            prop = _match_proposition(propositions, leaned) if leaned else None
            if prop is None or not prop.fully_resolved:
                result.review.append(
                    ReviewItem("no_matching_game", f"{sp.matchup}: {why}", sp, prop)
                )
                continue
            others = [c for c in prop.team_codes if c != leaned]
            if len(others) != 1:
                result.review.append(ReviewItem("ambiguous_opponent", sp.matchup, sp, prop))
                continue
            code = others[0]
        elif code is None:
            result.review.append(ReviewItem("untranslatable", f"{sp.matchup}: {why}", sp))
            continue

        prop = _match_proposition(propositions, code)
        if prop is None:
            result.review.append(
                ReviewItem("no_matching_game", f"no proposition contains {code}", sp)
            )
            continue
        if not prop.fully_resolved:
            result.review.append(
                ReviewItem(
                    "unresolved_proposition",
                    f"proposition {prop.id} has options ffm could not map to teams "
                    f"({[o.label for o in prop.options]})",
                    sp,
                    prop,
                )
            )
            continue
        if prop.id in seen_props:
            result.review.append(
                ReviewItem("duplicate_pick", f"two staff picks map to game {prop.label}", sp, prop)
            )
            continue
        if prop.is_locked():
            result.review.append(ReviewItem("locked", f"{prop.label} already kicked off", sp, prop))
            continue

        option = prop.option_for(code)
        if option is None:
            result.review.append(ReviewItem("no_option", f"{code} not selectable", sp, prop))
            continue

        seen_props.add(prop.id)
        result.picks.append(Pick(proposition_id=prop.id, option_id=option.id, team_code=code))
        result.confidence_by_prop[prop.id] = prob
        if abs(prob - 0.5) < COINFLIP_EDGE:
            log.info("coin-flip pick: %s (p=%.3f) — %s", prop.label, prob, why)

    if use_confidence_points:
        assign_confidence_points(result)
    return result


def assign_confidence_points(result: JoinResult) -> None:
    """Assign 1..N confidence points, highest to the strongest edge.

    Mutates ``result.picks`` in place. Pools that don't use confidence points
    simply never call this.
    """
    ordered = sorted(
        result.picks,
        key=lambda p: result.confidence_by_prop.get(p.proposition_id, 0.5),
        reverse=True,
    )
    total = len(ordered)
    for rank, pick in enumerate(ordered):
        pick.confidence = total - rank
