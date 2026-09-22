"""The claim-level bid market.

The point of this module is that losing bids carry information the winning bid
alone doesn't: a $40 winner over a $6 runner-up means someone overpaid an empty
room, while $40 over $38 means that is genuinely what the player costs.
"""

import pytest

from fcc.engines.market import MIN_CLAIMS_FOR_SIGNAL, BidMarket, Claim, parse_claims

PLAYERS = {
    "te1": {"full_name": "Star TE", "position": "TE"},
    "te2": {"full_name": "Other TE", "position": "TE"},
    "rb1": {"full_name": "Some RB", "position": "RB"},
}


def waiver(player_id, bid, status="complete", week=3):
    return {
        "type": "waiver",
        "status": status,
        "leg": week,
        "adds": {player_id: 1},
        "settings": {"waiver_bid": bid},
    }


# --- parsing ---------------------------------------------------------------
def test_bids_on_one_player_group_into_a_single_claim():
    claims = parse_claims(
        [
            waiver("te1", 40),
            waiver("te1", 38, status="failed"),
            waiver("te1", 12, status="failed"),
        ],
        PLAYERS,
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim.winning_bid == 40
    assert sorted(claim.losing_bids) == [12, 38]
    assert claim.runner_up == 38
    assert claim.bidder_count == 3
    assert claim.player_name == "Star TE"
    assert claim.position == "TE"


def test_margin_and_overpay_distinguish_a_soft_room_from_a_contested_one():
    soft = parse_claims([waiver("te1", 40), waiver("te1", 6, status="failed")], PLAYERS)[0]
    contested = parse_claims([waiver("te2", 40), waiver("te2", 38, status="failed")], PLAYERS)[0]

    assert soft.margin == 34
    assert contested.margin == 2
    assert soft.overpay_ratio > contested.overpay_ratio


def test_an_uncontested_claim_has_no_runner_up():
    claim = parse_claims([waiver("te1", 5)], PLAYERS)[0]
    assert not claim.contested
    assert claim.runner_up is None
    assert claim.margin is None


def test_claims_are_separated_by_week():
    claims = parse_claims([waiver("te1", 10, week=2), waiver("te1", 30, week=5)], PLAYERS)
    assert len(claims) == 2
    assert {c.week for c in claims} == {2, 5}


def test_non_waiver_and_malformed_rows_are_skipped():
    rows = [
        {"type": "free_agent", "status": "complete", "adds": {"te1": 1}, "settings": {}},
        {"type": "trade", "status": "complete"},
        {"type": "waiver", "status": "complete", "adds": {}, "settings": {"waiver_bid": 9}},
        {"type": "waiver", "status": "complete", "adds": {"te1": 1}, "settings": {}},
        {"type": "waiver", "status": "complete", "adds": {"te1": 1},
         "settings": {"waiver_bid": "abc"}},
        {},
    ]
    assert parse_claims(rows, PLAYERS) == []


def test_a_drop_only_waiver_is_not_a_claim():
    assert parse_claims([{"type": "waiver", "status": "complete", "drops": {"te1": 1},
                         "settings": {"waiver_bid": 5}}], PLAYERS) == []


# --- win probability -------------------------------------------------------
def market(*winning_bids, position="TE"):
    return BidMarket(
        claims=[
            Claim(player_id=f"p{i}", position=position, winning_bid=b)
            for i, b in enumerate(winning_bids)
        ]
    )


def test_win_probability_is_the_share_of_claims_the_bid_beats():
    m = market(5, 10, 20, 40)
    assert m.win_probability(0) == 0.0
    assert m.win_probability(11) == 0.5      # beats 5 and 10
    assert m.win_probability(100) == 1.0


def test_matching_the_winning_bid_counts_as_half():
    """Sleeper settles ties on waiver priority, so matching is a coin flip."""
    assert market(20).win_probability(20) == 0.5
    assert market(20).win_probability(21) == 1.0


def test_win_probability_is_none_without_resolved_claims():
    assert BidMarket().win_probability(10) is None
    unresolved = BidMarket(claims=[Claim(player_id="x", losing_bids=[5])])
    assert unresolved.win_probability(10) is None


def test_price_for_win_probability_is_the_cheapest_bid_that_clears_it():
    m = market(5, 10, 20, 40)
    # $10 beats 5 and ties 10 -> 1.5/4 = 0.375, short of half. $11 clears both.
    assert m.win_probability(10) == 0.375
    assert m.price_for_win_probability(0.5, 100) == 11
    # And the price it returns really does hit the target it was asked for.
    for target in (0.25, 0.5, 0.75, 0.9):
        price = m.price_for_win_probability(target, 100)
        assert m.win_probability(price) >= target
        assert m.win_probability(price - 1) < target      # genuinely the cheapest


def test_price_for_win_probability_is_monotonic():
    m = market(3, 9, 14, 22, 35)
    prices = [m.price_for_win_probability(p, 100) for p in (0.25, 0.5, 0.75, 0.9)]
    assert prices == sorted(prices)


# --- segmentation ----------------------------------------------------------
def test_comparable_prefers_the_position_when_the_sample_supports_it():
    claims = [Claim(player_id=f"te{i}", position="TE", winning_bid=10 + i)
              for i in range(MIN_CLAIMS_FOR_SIGNAL)]
    claims += [Claim(player_id="rb1", position="RB", winning_bid=99)]
    segment = BidMarket(claims=claims).comparable("TE")
    assert "TE" in segment.segment
    assert 99 not in [c.winning_bid for c in segment.resolved]


def test_comparable_widens_rather_than_answering_off_two_data_points():
    claims = [
        Claim(player_id="te1", position="TE", winning_bid=10),
        *[Claim(player_id=f"rb{i}", position="RB", winning_bid=20) for i in range(6)],
    ]
    segment = BidMarket(claims=claims).comparable("TE")
    assert "too few" in segment.segment
    assert len(segment.resolved) == 7


def test_the_segment_used_is_always_reported():
    """A number without its sample is not an answer."""
    for pos in ("TE", None):
        seg = BidMarket(claims=[Claim(player_id="x", position="TE", winning_bid=5)]).comparable(pos)
        assert seg.segment
        assert seg.summary()["segment"] == seg.segment


# --- summary ---------------------------------------------------------------
def test_summary_reports_both_sides_of_the_market():
    claims = parse_claims(
        [
            waiver("te1", 40), waiver("te1", 38, status="failed"),
            waiver("te2", 12), waiver("te2", 2, status="failed"),
            waiver("rb1", 8),
        ],
        PLAYERS,
    )
    s = BidMarket(claims=claims).summary()
    assert s["claims"] == 3
    assert s["resolved"] == 3
    assert s["median_winning_bid"] == 12
    assert s["median_runner_up"] == 20        # median of [38, 2]
    assert s["contest_rate"] == pytest.approx(2 / 3)
    assert s["median_overpay"] is not None


def test_a_thin_market_reports_itself_as_unusable():
    assert not BidMarket(claims=[Claim(player_id="x", winning_bid=5)]).usable
    assert BidMarket(
        claims=[Claim(player_id=f"p{i}", winning_bid=i + 1)
                for i in range(MIN_CLAIMS_FOR_SIGNAL)]
    ).usable


def test_empty_market_summarises_without_crashing():
    s = BidMarket().summary()
    assert s["claims"] == 0
    assert s["median_winning_bid"] is None
    assert s["usable"] is False
