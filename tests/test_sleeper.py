"""Sleeper read connector.

The alignment test is the one that matters. Sleeper's `starters` array is
positional against the league's non-bench `roster_positions`; if that mapping
slips, every player is labelled with the wrong slot and the Stage 3 guardian
would propose illegal swaps while looking perfectly correct.
"""

import json

import httpx
import pytest
import respx

from fcc.platforms.base import Player, PlayerStatus
from fcc.platforms.sleeper import BASE, SleeperClient, SleeperError, build_slots

ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "IR"]

PLAYERS = {
    "p_qb": {"full_name": "Test QB", "position": "QB", "team": "KC"},
    "p_rb1": {"full_name": "Test RB1", "position": "RB", "team": "BAL"},
    "p_rb2": {"full_name": "Test RB2", "position": "RB", "team": "SF", "injury_status": "Out"},
    "p_wr1": {"full_name": "Test WR1", "position": "WR", "team": "SEA"},
    "p_wr2": {"full_name": "Test WR2", "position": "WR", "team": "WSH"},
    "p_te": {"full_name": "Test TE", "position": "TE", "team": "DAL"},
    "p_flex": {"full_name": "Test Flex", "position": "RB", "team": "GB"},
    "p_k": {"full_name": "Test K", "position": "K", "team": "NE"},
    "p_bench": {"full_name": "Bench Guy", "position": "RB", "team": "JAC"},
    "KC": {"position": "DEF", "team": "KC"},
}

STARTERS = ["p_qb", "p_rb1", "p_rb2", "p_wr1", "p_wr2", "p_te", "p_flex", "p_k", "KC"]
ALL_PLAYERS = [*STARTERS, "p_bench"]


def _lookup(pid):
    raw = PLAYERS.get(pid, {})
    from fcc.platforms.base import normalize_status

    return Player(
        player_id=pid,
        name=raw.get("full_name") or raw.get("team") or pid,
        position=raw.get("position"),
        team=raw.get("team"),
        status=normalize_status(raw.get("injury_status")),
    )


# --- slot alignment --------------------------------------------------------
def test_starters_align_positionally_with_roster_positions():
    slots = build_slots(ROSTER_POSITIONS, STARTERS, ALL_PLAYERS, _lookup)
    starters = [s for s in slots if s.starter]
    assert [s.slot for s in starters] == [
        "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF",
    ]
    assert [s.player.player_id for s in starters] == STARTERS


def test_bench_gets_everyone_not_started():
    slots = build_slots(ROSTER_POSITIONS, STARTERS, ALL_PLAYERS, _lookup)
    bench = [s for s in slots if not s.starter]
    assert [s.player.player_id for s in bench] == ["p_bench"]
    assert all(s.slot == "BN" for s in bench)


def test_empty_starting_slot_is_represented_not_dropped():
    """An empty slot must stay in the lineup, or slot alignment shifts."""
    starters = list(STARTERS)
    starters[2] = "0"  # Sleeper's sentinel for an unfilled slot
    slots = build_slots(ROSTER_POSITIONS, starters, ALL_PLAYERS, _lookup)
    active = [s for s in slots if s.starter]
    assert len(active) == 9
    assert active[2].slot == "RB"
    assert active[2].empty
    # The slots after the gap must not have shifted up.
    assert active[3].player.player_id == "p_wr1"


def test_short_starters_array_does_not_misalign_or_crash():
    slots = build_slots(ROSTER_POSITIONS, STARTERS[:4], ALL_PLAYERS, _lookup)
    active = [s for s in slots if s.starter]
    assert len(active) == 9
    assert [s.slot for s in active[:4]] == ["QB", "RB", "RB", "WR"]
    assert all(s.empty for s in active[4:])


def test_bench_and_ir_positions_never_become_starting_slots():
    slots = build_slots(ROSTER_POSITIONS, STARTERS, ALL_PLAYERS, _lookup)
    assert not any(s.starter and s.slot in {"BN", "IR", "TAXI"} for s in slots)


def test_injured_starter_is_surfaced_as_a_problem():
    from fcc.platforms.base import Roster

    roster = Roster(league_key="k", team_id="1", slots=build_slots(
        ROSTER_POSITIONS, STARTERS, ALL_PLAYERS, _lookup
    ))
    problems = roster.problem_starters()
    assert [s.player.player_id for s in problems] == ["p_rb2"]
    assert problems[0].slot == "RB"


# --- HTTP behaviour --------------------------------------------------------
LEAGUE = {
    "name": "Main Sleeper League",
    "season": "2026",
    "roster_positions": ROSTER_POSITIONS,
    "settings": {"waiver_budget": 100},
}
ROSTERS = [
    {
        "roster_id": 1,
        "owner_id": "user-me",
        "starters": STARTERS,
        "players": ALL_PLAYERS,
        "settings": {"wins": 7, "losses": 3, "ties": 0, "waiver_budget_used": 35},
    },
    {"roster_id": 2, "owner_id": "user-them", "starters": [], "players": [], "settings": {}},
]
USERS = [{"user_id": "user-me", "display_name": "ryan", "metadata": {"team_name": "My Squad"}}]


@pytest.fixture
def mocked(tmp_path):
    # assert_all_called=False: not every test exercises every endpoint, and a
    # teardown failure would obscure the assertion the test actually makes.
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/state/nfl", name="state").mock(
            return_value=httpx.Response(200, json={"week": 5})
        )
        mock.get("/league/L1", name="league").mock(
            return_value=httpx.Response(200, json=LEAGUE)
        )
        mock.get("/league/L1/rosters", name="rosters").mock(
            return_value=httpx.Response(200, json=ROSTERS)
        )
        mock.get("/league/L1/users", name="users").mock(
            return_value=httpx.Response(200, json=USERS)
        )
        mock.get("/players/nfl", name="players").mock(
            return_value=httpx.Response(200, json=PLAYERS)
        )
        yield mock


def _client(tmp_path):
    return SleeperClient(
        league_id="L1", league_key="sleeper_main", user_id="user-me", cache_dir=tmp_path
    )


def test_roster_is_built_from_the_api(mocked, tmp_path):
    roster = _client(tmp_path).roster()
    assert roster.team_name == "My Squad"
    assert roster.week == 5
    assert len(roster.starters) == 9
    assert [s.player.name for s in roster.problem_starters()] == ["Test RB2"]


def test_team_codes_are_normalized_from_sleeper_spellings(mocked, tmp_path):
    roster = _client(tmp_path).roster()
    teams = {s.player.team for s in roster.slots if s.player}
    assert "WAS" in teams and "WSH" not in teams   # Sleeper says WSH
    assert "JAX" in teams and "JAC" not in teams


def test_league_summary_computes_remaining_faab(mocked, tmp_path):
    summary = _client(tmp_path).league_summary()
    assert summary.record == "7-3"
    assert summary.faab_remaining == 65      # 100 budget - 35 used
    assert summary.name == "Main Sleeper League"


def test_wrong_user_id_fails_loudly(mocked, tmp_path):
    client = SleeperClient(league_id="L1", user_id="nobody", cache_dir=tmp_path)
    with pytest.raises(SleeperError, match="sleeper_user_id"):
        client.roster()


def test_player_index_is_cached_to_disk_not_refetched(mocked, tmp_path):
    client = _client(tmp_path)
    client.players()
    client._players = None          # drop the in-memory copy
    client.players()
    assert mocked["players"].call_count == 1
    assert json.loads((tmp_path / "players_nfl.json").read_text())["p_qb"]["position"] == "QB"


def test_unknown_player_id_is_not_reported_as_healthy(mocked, tmp_path):
    """A player the index doesn't know must not read as startable."""
    player = _client(tmp_path).player("no-such-id")
    assert player.status is PlayerStatus.UNKNOWN
    assert player.status.is_doubtful


def test_missing_league_is_a_clear_error(tmp_path):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/league/NOPE").mock(return_value=httpx.Response(404))
        with pytest.raises(SleeperError, match="check the league id"):
            SleeperClient(league_id="NOPE", cache_dir=tmp_path).league()
