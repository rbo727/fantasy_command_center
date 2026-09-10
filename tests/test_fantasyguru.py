"""FantasyGuru extraction.

The LLM call is stubbed. What's under test is the wiring around it: that HTML
becomes readable text, that the model's output converts into StaffPick objects
with the sign convention intact, and that the whole path feeds the pick'em
engine correctly.
"""

from dataclasses import dataclass

import pytest

from fcc.engines.pickem import join, straight_up_side
from fcc.platforms.espn_pickem import Option, Proposition
from fcc.sources.fantasyguru import (
    EXTRACTION_SYSTEM,
    ExtractedPick,
    ExtractedPicks,
    FantasyGuruError,
    extract_staff_picks,
)
from fcc.sources.html_text import html_to_text

PAGE = """
<html><head><style>.a{color:red}</style><script>track();</script></head>
<body>
  <h1>Week 3 Staff Picks</h1>
  <p>We like the <b>Ravens</b> +7 at Kansas City — best bet of the week.</p>
  <table>
    <tr><th>Game</th><th>Pick</th></tr>
    <tr><td>SF@SEA</td><td>49ers -3</td></tr>
  </table>
</body></html>
"""


@dataclass
class _StubResponse:
    parsed_output: ExtractedPicks


class StubClient:
    """Stands in for anthropic.Anthropic, recording what it was asked."""

    def __init__(self, result: ExtractedPicks):
        self._result = result
        self.calls: list[dict] = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _StubResponse(self._result)


# --- html_to_text ----------------------------------------------------------
def test_html_to_text_drops_scripts_and_keeps_content():
    text = html_to_text(PAGE)
    assert "track()" not in text
    assert "color:red" not in text
    assert "Ravens" in text
    assert "49ers -3" in text


def test_html_to_text_separates_table_cells():
    text = html_to_text("<table><tr><td>SF@SEA</td><td>49ers -3</td></tr></table>")
    assert "SF@SEA" in text and "49ers -3" in text
    assert "SF@SEA49ers" not in text  # cells must not run together


# --- extraction ------------------------------------------------------------
def test_extraction_converts_to_staff_picks():
    stub = StubClient(
        ExtractedPicks(
            picks=[
                ExtractedPick(
                    matchup="BAL@KC", team="Ravens", market="ATS", spread=7.0, conviction=5
                ),
                ExtractedPick(
                    matchup="SF@SEA", team="49ers", market="ATS", spread=-3.0, conviction=3
                ),
            ],
            notes="",
        )
    )
    picks, notes = extract_staff_picks(PAGE, week=3, client=stub)

    assert [p.team for p in picks] == ["Ravens", "49ers"]
    assert [p.spread for p in picks] == [7.0, -3.0]
    assert picks[0].team_code == "BAL"
    assert notes == ""


def test_extraction_sends_stripped_text_not_raw_html():
    stub = StubClient(ExtractedPicks(picks=[]))
    extract_staff_picks(PAGE, week=3, client=stub)
    content = stub.calls[0]["messages"][0]["content"]
    assert "<script>" not in content
    assert "track()" not in content
    assert "Ravens" in content
    assert "week 3" in content


def test_extraction_uses_structured_output_schema():
    stub = StubClient(ExtractedPicks(picks=[]))
    extract_staff_picks(PAGE, client=stub)
    assert stub.calls[0]["output_format"] is ExtractedPicks
    assert stub.calls[0]["system"] == EXTRACTION_SYSTEM


def test_empty_page_raises_rather_than_calling_the_model():
    stub = StubClient(ExtractedPicks(picks=[]))
    with pytest.raises(FantasyGuruError, match="no readable text"):
        extract_staff_picks("<html><body></body></html>", client=stub)
    assert stub.calls == []


def test_paywalled_page_surfaces_notes_not_fabricated_picks():
    stub = StubClient(
        ExtractedPicks(picks=[], notes="This looks like a subscription wall, not a picks page.")
    )
    picks, notes = extract_staff_picks(PAGE, client=stub)
    assert picks == []
    assert "subscription wall" in notes


def test_conviction_is_bounded():
    """Guards the schema: the engine weights by conviction, so 1-5 must hold."""
    with pytest.raises(ValueError):
        ExtractedPick(matchup="A@B", team="X", market="ML", conviction=9)


# --- end to end through the engine ----------------------------------------
def test_extracted_picks_flow_through_to_submittable_picks():
    """The whole Stage 1 chain, minus the network at both ends."""
    stub = StubClient(
        ExtractedPicks(
            picks=[
                # Staff like a +7 dog against the spread. Straight up, that's KC.
                ExtractedPick(
                    matchup="BAL@KC", team="Ravens", market="ATS", spread=7.0, conviction=5
                ),
                ExtractedPick(
                    matchup="SF@SEA", team="49ers", market="ATS", spread=-3.0, conviction=3
                ),
            ]
        )
    )
    picks, _ = extract_staff_picks(PAGE, week=3, client=stub)

    slate = [
        Proposition(
            id="p1",
            week=3,
            options=[
                Option(id="o-bal", team_code="BAL", label="BAL"),
                Option(id="o-kc", team_code="KC", label="KC"),
            ],
        ),
        Proposition(
            id="p2",
            week=3,
            options=[
                Option(id="o-sf", team_code="SF", label="SF"),
                Option(id="o-sea", team_code="SEA", label="SEA"),
            ],
        ),
    ]
    result = join(picks, slate)

    assert result.clean, result.review
    assert {p.team_code for p in result.picks} == {"KC", "SF"}
    # The ATS underdog lean became a pick for the other side, with the right id.
    kc = next(p for p in result.picks if p.team_code == "KC")
    assert kc.option_id == "o-kc"


def test_sign_convention_is_the_one_the_engine_expects():
    """A flipped spread sign inverts the pick, so pin the convention here."""
    favourite = ExtractedPick(matchup="BAL@KC", team="Chiefs", market="ATS", spread=-6.5)
    underdog = ExtractedPick(matchup="BAL@KC", team="Ravens", market="ATS", spread=6.5)

    from fcc.engines.pickem import StaffPick

    fav_code, _, _ = straight_up_side(StaffPick(**favourite.model_dump()))
    dog_code, _, why = straight_up_side(StaffPick(**underdog.model_dump()))

    assert fav_code == "KC"          # laying points -> they're favoured
    assert dog_code is None          # getting points -> not a winner pick
    assert "other side" in why
