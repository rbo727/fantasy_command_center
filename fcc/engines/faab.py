"""FAAB bid recommendations.

Generic "spend 30% on a league-winning RB" charts are calibrated to a league
that isn't yours. A ten-team league where nobody bids hard and a twelve-team
league of sharks price the same player completely differently, so the model here
starts from a defensible baseline and then **calibrates against your league's own
winning bids**, parsed from its transaction history.

Every adjustment is recorded in the rationale. A bid you can't explain is a bid
you shouldn't approve — and FAAB is money tier, so a human always approves it
anyway (`fcc/core/actions.py`).

The refusal matters as much as the number: with no projection for a player there
is no value over replacement, and :func:`recommend_bid` returns ``None`` rather
than inventing a figure that would look authoritative in the approval queue.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Baseline bid as a fraction of *remaining* budget, by value over replacement
#: in projected points per week. Anchors chosen to match widely-reported
#: outcomes: a marginal streamer goes for a few percent, a genuine every-week
#: starter for a quarter of a budget, a league-altering add for around half.
#: Interpolated linearly between anchors, flat outside them.
BASELINE_CURVE: list[tuple[float, float]] = [
    (0.0, 0.00),
    (1.0, 0.02),
    (2.0, 0.05),
    (3.0, 0.10),
    (5.0, 0.22),
    (8.0, 0.40),
    (12.0, 0.55),
]

#: How much roster need moves a bid.
NEED_MULTIPLIERS = {
    "replacing_injured_starter": 1.35,
    "starter_upgrade": 1.15,
    "depth": 0.80,
    "speculative": 0.55,
}

#: Never let a single bid take the budget below this fraction of what remains,
#: unless the caller explicitly allows it — running dry in October is its own
#: kind of loss.
DEFAULT_RESERVE_FRACTION = 0.15

#: A market factor outside this band is treated as too thin to trust.
MARKET_FACTOR_BOUNDS = (0.5, 2.0)

#: Below this many observed bids, league history is a curiosity, not a signal.
MIN_BIDS_FOR_CALIBRATION = 5


@dataclass
class BidContext:
    """Everything about *your* situation that changes the number."""

    budget_total: int
    budget_remaining: int
    weeks_remaining: int
    need: str = "depth"
    #: Allow a bid that breaks the reserve floor (a genuine league-winner).
    allow_all_in: bool = False

    def __post_init__(self) -> None:
        if self.need not in NEED_MULTIPLIERS:
            log.warning("unknown need %r; treating as depth", self.need)
            self.need = "depth"

    @property
    def spent_fraction(self) -> float:
        if self.budget_total <= 0:
            return 0.0
        return 1 - (self.budget_remaining / self.budget_total)


@dataclass
class MarketHistory:
    """Winning bids actually observed in this league, as budget fractions."""

    winning_fractions: list[float] = field(default_factory=list)

    @property
    def sample_size(self) -> int:
        return len(self.winning_fractions)

    @property
    def usable(self) -> bool:
        return self.sample_size >= MIN_BIDS_FOR_CALIBRATION

    def percentile(self, p: float) -> float | None:
        """The p-th percentile (0-100) of winning bids, or None if too thin."""
        if not self.winning_fractions:
            return None
        data = sorted(self.winning_fractions)
        if len(data) == 1:
            return data[0]
        rank = (p / 100) * (len(data) - 1)
        low = int(rank)
        high = min(low + 1, len(data) - 1)
        return data[low] + (data[high] - data[low]) * (rank - low)

    def factor(self, baseline_median: float = 0.05) -> float:
        """How much hotter or colder this league runs than the baseline.

        Clamped, and 1.0 (no adjustment) when the sample is too small — a
        couple of lucky $1 claims must not convince the model the league is
        asleep.
        """
        if not self.usable:
            return 1.0
        median = statistics.median(self.winning_fractions)
        if median <= 0 or baseline_median <= 0:
            return 1.0
        low, high = MARKET_FACTOR_BOUNDS
        return max(low, min(high, median / baseline_median))


@dataclass
class BidRecommendation:
    low: int
    target: int
    high: int
    max_justifiable: int
    confidence: float
    rationale: str
    factors: dict = field(default_factory=dict)

    def describe(self) -> str:
        return f"${self.low}-${self.high} (target ${self.target})"


def baseline_fraction(vor_per_week: float) -> float:
    """Interpolate the baseline curve at *vor_per_week*."""
    if vor_per_week <= BASELINE_CURVE[0][0]:
        return BASELINE_CURVE[0][1]
    if vor_per_week >= BASELINE_CURVE[-1][0]:
        return BASELINE_CURVE[-1][1]
    for (x0, y0), (x1, y1) in zip(BASELINE_CURVE, BASELINE_CURVE[1:], strict=False):
        if x0 <= vor_per_week <= x1:
            span = x1 - x0
            return y0 if span == 0 else y0 + (y1 - y0) * (vor_per_week - x0) / span
    return BASELINE_CURVE[-1][1]


def parse_winning_bids(transactions: list[dict], budget_total: int) -> MarketHistory:
    """Extract successful waiver bids from a league's transaction log.

    Sleeper reports every attempt, including the losing ones; only completed
    waivers tell you what it actually took to win a player, so failed bids are
    dropped rather than dragging the median down.
    """
    fractions: list[float] = []
    if budget_total <= 0:
        return MarketHistory()

    for txn in transactions or []:
        if txn.get("type") != "waiver" or txn.get("status") != "complete":
            continue
        bid = (txn.get("settings") or {}).get("waiver_bid")
        if bid is None:
            continue
        try:
            value = float(bid)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        fractions.append(value / budget_total)

    return MarketHistory(winning_fractions=fractions)


def recommend_bid(
    vor_per_week: float | None,
    context: BidContext,
    market: MarketHistory | None = None,
) -> BidRecommendation | None:
    """Recommend a bid range, or ``None`` when there is nothing to reason from.

    ``vor_per_week`` is the player's projected points per week above what you
    could get for free at that position. ``None`` means no projection was
    available — the caller should route that to review rather than guess.
    """
    if vor_per_week is None:
        log.info("no projection available; refusing to invent a bid")
        return None
    if context.budget_remaining <= 0:
        return BidRecommendation(
            low=0,
            target=0,
            high=0,
            max_justifiable=0,
            confidence=1.0,
            rationale="No FAAB budget remaining.",
            factors={"budget_remaining": 0},
        )

    # None means "the caller is handling the market separately" (the CLI shows
    # it as its own table); an empty MarketHistory means "we looked and there
    # was nothing". Those must not produce the same rationale.
    market_omitted = market is None
    market = market or MarketHistory()
    base = baseline_fraction(vor_per_week)
    need_multiplier = NEED_MULTIPLIERS[context.need]
    market_factor = market.factor()

    # Budget that is not spent by the end of the season was wasted, so late in
    # the year the same player is worth proportionally more of what is left.
    urgency = 1.0
    if context.weeks_remaining > 0:
        urgency = min(1.6, max(0.85, 10 / max(context.weeks_remaining, 1)))

    fraction = base * need_multiplier * market_factor * urgency
    target = fraction * context.budget_remaining

    # Keep something back for the rest of the season unless told otherwise.
    ceiling = float(context.budget_remaining)
    if not context.allow_all_in:
        ceiling = context.budget_remaining * (1 - DEFAULT_RESERVE_FRACTION)
    ceiling = max(1.0, ceiling)
    target = min(target, ceiling)

    low = max(1, round(target * 0.7))
    high = max(low, round(min(target * 1.35, ceiling)))
    target_int = max(1, min(round(target), high))

    confidence = 0.75 if (market.usable or market_omitted) else 0.45
    if vor_per_week <= 0:
        confidence = min(confidence, 0.3)

    bits = [
        f"{vor_per_week:+.1f} pts/wk over replacement -> baseline "
        f"{base:.0%} of remaining budget",
        f"need '{context.need}' x{need_multiplier:.2f}",
    ]
    if market.usable:
        bits.append(
            f"league market x{market_factor:.2f} (median winning bid "
            f"{statistics.median(market.winning_fractions):.0%} of budget "
            f"across {market.sample_size} claims)"
        )
    elif market_omitted:
        bits.append("value only - market priced separately")
    else:
        bits.append(
            f"no league calibration ({market.sample_size} prior bids, "
            f"need {MIN_BIDS_FOR_CALIBRATION}) - using the generic baseline"
        )
    if urgency != 1.0:
        bits.append(f"{context.weeks_remaining} weeks left x{urgency:.2f}")
    if not context.allow_all_in:
        bits.append(f"holding back {DEFAULT_RESERVE_FRACTION:.0%} of ${context.budget_remaining}")

    return BidRecommendation(
        low=low,
        target=target_int,
        high=high,
        max_justifiable=round(ceiling),
        confidence=confidence,
        rationale="; ".join(bits),
        factors={
            "baseline_fraction": round(base, 4),
            "need_multiplier": need_multiplier,
            "market_factor": round(market_factor, 4),
            "urgency": round(urgency, 4),
            "budget_remaining": context.budget_remaining,
            "market_sample_size": market.sample_size,
        },
    )
