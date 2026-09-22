"""FAAB bid recommendations.

The properties worth pinning are the guardrails, not the arithmetic: the model
must never recommend more money than exists, must refuse rather than invent a
number when it has nothing to reason from, and must not let a thin sample of
league history swing it.
"""

import pytest

from fcc.engines.faab import (
    DEFAULT_RESERVE_FRACTION,
    MIN_BIDS_FOR_CALIBRATION,
    BidContext,
    MarketHistory,
    baseline_fraction,
    parse_winning_bids,
    recommend_bid,
)


def ctx(remaining=100, total=100, weeks=10, need="depth", all_in=False):
    return BidContext(
        budget_total=total,
        budget_remaining=remaining,
        weeks_remaining=weeks,
        need=need,
        allow_all_in=all_in,
    )


# --- the baseline curve ----------------------------------------------------
def test_baseline_is_monotonic_in_value():
    """A better player is never worth less."""
    fractions = [baseline_fraction(v) for v in range(0, 15)]
    assert fractions == sorted(fractions)


def test_baseline_is_flat_outside_the_anchors():
    assert baseline_fraction(-5) == baseline_fraction(0) == 0.0
    assert baseline_fraction(50) == baseline_fraction(12)


def test_baseline_interpolates_between_anchors():
    # Anchors at (2.0, 0.05) and (3.0, 0.10); halfway is 0.075.
    assert baseline_fraction(2.5) == pytest.approx(0.075)


# --- refusals and guardrails ----------------------------------------------
def test_no_projection_means_no_recommendation():
    """Better an empty approval queue entry than an authoritative-looking guess."""
    assert recommend_bid(None, ctx()) is None


def test_zero_budget_recommends_nothing_and_says_why():
    rec = recommend_bid(8.0, ctx(remaining=0))
    assert (rec.low, rec.target, rec.high) == (0, 0, 0)
    assert "No FAAB budget remaining" in rec.rationale


def test_bid_never_exceeds_remaining_budget():
    for remaining in (1, 5, 23, 100):
        rec = recommend_bid(12.0, ctx(remaining=remaining, need="replacing_injured_starter"))
        assert rec.high <= remaining
        assert rec.target <= remaining


def test_a_reserve_is_held_back_by_default():
    rec = recommend_bid(12.0, ctx(remaining=100))
    assert rec.high <= 100 * (1 - DEFAULT_RESERVE_FRACTION)
    assert "holding back" in rec.rationale


def test_all_in_is_possible_but_must_be_asked_for():
    held = recommend_bid(12.0, ctx(remaining=100))
    all_in = recommend_bid(12.0, ctx(remaining=100, all_in=True))
    assert all_in.max_justifiable > held.max_justifiable
    assert all_in.max_justifiable == 100


def test_low_never_exceeds_high():
    for vor in (0.0, 1.0, 4.0, 9.0, 20.0):
        for remaining in (1, 3, 50, 200):
            rec = recommend_bid(vor, ctx(remaining=remaining, total=200))
            assert rec.low <= rec.target <= rec.high


def test_a_worthless_player_gets_low_confidence():
    rec = recommend_bid(-1.0, ctx())
    assert rec.confidence <= 0.3


# --- roster need -----------------------------------------------------------
def test_replacing_an_injured_starter_bids_more_than_speculation():
    starter = recommend_bid(5.0, ctx(need="replacing_injured_starter")).target
    depth = recommend_bid(5.0, ctx(need="depth")).target
    spec = recommend_bid(5.0, ctx(need="speculative")).target
    assert starter > depth > spec


def test_an_unknown_need_degrades_to_depth_rather_than_erroring():
    context = BidContext(budget_total=100, budget_remaining=100, weeks_remaining=10, need="???")
    assert context.need == "depth"


# --- league calibration ----------------------------------------------------
def test_a_thin_sample_is_ignored():
    """Two lucky $1 claims must not convince the model the league is asleep."""
    thin = MarketHistory(winning_fractions=[0.01, 0.01])
    assert not thin.usable
    assert thin.factor() == 1.0


def test_a_hot_league_raises_the_bid():
    hot = MarketHistory(winning_fractions=[0.20] * MIN_BIDS_FOR_CALIBRATION)
    cold = MarketHistory(winning_fractions=[0.01] * MIN_BIDS_FOR_CALIBRATION)
    assert recommend_bid(5.0, ctx(), hot).target > recommend_bid(5.0, ctx(), cold).target


def test_market_factor_is_clamped_in_both_directions():
    absurd_high = MarketHistory(winning_fractions=[0.95] * 10)
    absurd_low = MarketHistory(winning_fractions=[0.0001] * 10)
    assert absurd_high.factor() <= 2.0
    assert absurd_low.factor() >= 0.5


def test_rationale_names_the_calibration_or_its_absence():
    calibrated = recommend_bid(5.0, ctx(), MarketHistory([0.1] * 10))
    assert "league market" in calibrated.rationale
    uncalibrated = recommend_bid(5.0, ctx(), MarketHistory([0.1]))
    assert "no league calibration" in uncalibrated.rationale
    assert calibrated.confidence > uncalibrated.confidence


def test_percentile_handles_small_and_empty_samples():
    assert MarketHistory([]).percentile(50) is None
    assert MarketHistory([0.2]).percentile(50) == 0.2
    assert MarketHistory([0.0, 1.0]).percentile(50) == pytest.approx(0.5)


# --- parsing the league's transaction log ---------------------------------
TRANSACTIONS = [
    {"type": "waiver", "status": "complete", "settings": {"waiver_bid": 25}},
    {"type": "waiver", "status": "complete", "settings": {"waiver_bid": 3}},
    # A losing bid: reported, but it is not what it took to win.
    {"type": "waiver", "status": "failed", "settings": {"waiver_bid": 99}},
    # Free agent pickups carry no bid.
    {"type": "free_agent", "status": "complete", "settings": {}},
    {"type": "trade", "status": "complete"},
]


def test_only_winning_waiver_bids_are_counted():
    market = parse_winning_bids(TRANSACTIONS, budget_total=100)
    assert sorted(market.winning_fractions) == [0.03, 0.25]


def test_failed_bids_do_not_drag_the_median():
    """Counting losing bids would systematically overstate the market."""
    assert 0.99 not in parse_winning_bids(TRANSACTIONS, 100).winning_fractions


def test_malformed_transactions_are_skipped_not_fatal():
    junk = [
        {"type": "waiver", "status": "complete", "settings": {"waiver_bid": "abc"}},
        {"type": "waiver", "status": "complete", "settings": {"waiver_bid": None}},
        {"type": "waiver", "status": "complete"},
        {"type": "waiver", "status": "complete", "settings": {"waiver_bid": -5}},
        {},
    ]
    assert parse_winning_bids(junk, 100).winning_fractions == []


def test_zero_budget_league_yields_no_market():
    assert parse_winning_bids(TRANSACTIONS, budget_total=0).winning_fractions == []


def test_parsing_an_empty_log_is_safe():
    assert parse_winning_bids([], 100).sample_size == 0
    assert parse_winning_bids(None, 100).sample_size == 0


# --- end to end ------------------------------------------------------------
def test_a_realistic_recommendation_is_explainable():
    market = parse_winning_bids(
        [
            {"type": "waiver", "status": "complete", "settings": {"waiver_bid": b}}
            for b in (18, 22, 30, 12, 25, 40)
        ],
        budget_total=100,
    )
    rec = recommend_bid(
        5.5, ctx(remaining=64, weeks=9, need="replacing_injured_starter"), market
    )
    assert 1 <= rec.low <= rec.target <= rec.high <= 64
    # The rationale must contain enough to approve or reject on.
    for expected in ("over replacement", "need", "league market", "weeks left"):
        assert expected in rec.rationale
    assert rec.factors["market_sample_size"] == 6


def test_an_omitted_market_is_distinguished_from_an_empty_one():
    """'We didn't look here' and 'we looked and found nothing' must differ."""
    omitted = recommend_bid(5.0, ctx(), None)
    empty = recommend_bid(5.0, ctx(), MarketHistory([]))

    assert "priced separately" in omitted.rationale
    assert "no league calibration" not in omitted.rationale
    assert "no league calibration" in empty.rationale
    # And an intentional omission must not be scored as missing evidence.
    assert omitted.confidence > empty.confidence
