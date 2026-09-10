"""Team resolution must be exact or absent — never a guess."""

import pytest

from fcc.core.teams import TEAMS, all_codes, normalize, parse_matchup, require


def test_thirty_two_teams():
    assert len(all_codes()) == 32


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # canonical
        ("KC", "KC"),
        ("BAL", "BAL"),
        # platform variants that differ from nflverse
        ("WSH", "WAS"),   # ESPN
        ("JAC", "JAX"),   # ESPN
        ("LA", "LAR"),    # nflverse uses LA for the Rams
        ("OAK", "LV"),    # relocation
        ("SD", "LAC"),
        ("STL", "LAR"),
        # prose
        ("Ravens", "BAL"),
        ("the Chiefs", None),  # article not stripped -> refuse rather than guess
        ("Kansas City Chiefs", "KC"),
        ("49ers", "SF"),
        ("Niners", "SF"),
        ("Bucs", "TB"),
        ("Washington Football Team", "WAS"),
        # case and punctuation folding
        ("green bay", "GB"),
        ("N.Y. Giants", None),  # not a known alias -> refuse
        ("NYG", "NYG"),
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize("text", ["NY", "New York", "", None, "Toronto Argonauts"])
def test_refuses_ambiguous_or_unknown(text):
    """A wrong team is worse than no team: these must not resolve."""
    assert normalize(text) is None


def test_require_raises_on_miss():
    assert require("Ravens") == "BAL"
    with pytest.raises(ValueError, match="New York"):
        require("New York")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BAL@KC", ("BAL", "KC")),
        ("Ravens at Chiefs", ("BAL", "KC")),
        ("KC vs BAL", ("KC", "BAL")),
        ("SF-SEA", ("SF", "SEA")),
        ("Philadelphia Eagles @ Dallas Cowboys", ("PHI", "DAL")),
    ],
)
def test_parse_matchup(text, expected):
    assert parse_matchup(text) == expected


@pytest.mark.parametrize("text", ["New York at KC", "KC", "KC@KC", "gibberish@nonsense"])
def test_parse_matchup_refuses(text):
    assert parse_matchup(text) is None


def test_every_team_resolves_from_its_own_labels():
    """Guards against a typo in the table silently orphaning a team."""
    for code, team in TEAMS.items():
        assert normalize(code) == code
        assert normalize(team.full_name) == code
        # Nicknames are unique across the league; cities are not (NY, LA).
        assert normalize(team.nickname) == code
