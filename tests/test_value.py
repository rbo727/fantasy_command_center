"""Value over replacement.

The point of replacement level is that a projection alone doesn't price a
player: 12 points a week is a fine TE and a bad RB. In a guillotine league it
moves every week, because a shrinking field means fewer starting slots and a
higher bar to clear.
"""


from fcc.engines.value import (
    RankedPlayer,
    find,
    replacement_points,
    replacement_rank,
    upgrade_over,
    value_over_replacement,
)


def pool(position, *points):
    return [
        RankedPlayer(name=f"{position}{i}", position=position, projected_points=p)
        for i, p in enumerate(points, start=1)
    ]


# --- replacement level -----------------------------------------------------
def test_replacement_rank_scales_with_league_size():
    assert replacement_rank("QB", 12) == 13
    assert replacement_rank("QB", 4) == 5


def test_replacement_rank_accounts_for_flex_demand():
    """More RBs start than the two in the lineup, because flex eats them."""
    assert replacement_rank("RB", 12) > replacement_rank("QB", 12) * 2


def test_a_shrinking_guillotine_field_raises_the_bar():
    """The same projection is worth less in a four-team endgame."""
    twelve = replacement_rank("TE", 12)
    four = replacement_rank("TE", 4)
    assert four < twelve


def test_replacement_rank_never_goes_below_one():
    assert replacement_rank("TE", 0) >= 1


def test_replacement_points_picks_the_right_player():
    players = pool("TE", 20, 15, 12, 9, 7, 5, 4)
    # 12 teams x 1.2 TE starters -> replacement is TE #15; only 7 here.
    assert replacement_points(players, "TE", 12) is None
    # With 4 teams alive replacement is TE #6 -> 5.0 points.
    assert replacement_points(players, "TE", 4) == 5


def test_shallow_rankings_return_none_rather_than_the_last_player():
    """'I can't see that deep' must not become 'the worst guy I know about'."""
    players = pool("RB", 20, 18, 15)
    assert replacement_points(players, "RB", 12) is None


def test_unknown_position_yields_none():
    assert replacement_points(pool("RB", 10, 8), "TE", 4) is None


# --- VOR -------------------------------------------------------------------
def test_value_over_replacement_is_the_gap_to_free():
    players = pool("TE", 20, 15, 12, 9, 7, 5, 4)
    target = players[0]                       # 20 points
    assert value_over_replacement(target, players, 4) == 15   # 20 - 5


def test_the_same_projection_is_worth_more_at_a_thin_position():
    tes = pool("TE", 14, 6, 5, 4, 3, 2)
    rbs = pool("RB", 14, 13, 12, 12, 11, 11)
    te = RankedPlayer(name="X", position="TE", projected_points=14)
    rb = RankedPlayer(name="Y", position="RB", projected_points=14)
    assert value_over_replacement(te, tes, 2) > value_over_replacement(rb, rbs, 2)


def test_no_projection_means_no_value():
    players = pool("TE", 20, 15, 12, 9, 7, 5)
    unknown = RankedPlayer(name="?", position="TE", projected_points=None)
    assert value_over_replacement(unknown, players, 4) is None


def test_vor_is_none_when_replacement_is_unknown():
    assert value_over_replacement(
        RankedPlayer(name="X", position="RB", projected_points=18), pool("RB", 20, 18), 12
    ) is None


# --- upgrade over the incumbent -------------------------------------------
def test_upgrade_is_measured_against_who_he_actually_replaces():
    """With a healthy starter, the honest number is the gap to him, not to free."""
    add = RankedPlayer(name="Add", position="TE", projected_points=12)
    incumbent = RankedPlayer(name="Have", position="TE", projected_points=10)
    assert upgrade_over(add, incumbent) == 2


def test_no_incumbent_means_no_upgrade_number():
    add = RankedPlayer(name="Add", position="TE", projected_points=12)
    assert upgrade_over(add, None) is None
    assert upgrade_over(add, RankedPlayer(name="?", position="TE")) is None


# --- lookup ----------------------------------------------------------------
def test_find_prefers_an_exact_match():
    players = [
        RankedPlayer(name="Brock Bowers", position="TE"),
        RankedPlayer(name="Brock Bowers Jr", position="TE"),
    ]
    assert find(players, "Brock Bowers").name == "Brock Bowers"


def test_find_refuses_an_ambiguous_partial():
    players = [
        RankedPlayer(name="Mike Williams", position="WR"),
        RankedPlayer(name="Mike Williamson", position="WR"),
    ]
    assert find(players, "Mike William") is None


def test_find_returns_none_for_a_miss():
    assert find(pool("RB", 10), "Nobody") is None
