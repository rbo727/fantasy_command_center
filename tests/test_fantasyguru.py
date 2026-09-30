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
    FantasyGuruSession,
    extract_staff_picks,
    find_waiver_report_url,
    parse_projections_csv,
    parse_rankings_csv,
    parse_waiver_report,
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


# --- rankings board (CSV export) ------------------------------------------
BOARD_CSV = (
    "RANK,PLAYER,TEAM,BYE,OPP\n"
    "1,Josh Allen,BUF,7,LAC\n"
    "2,Lamar Jackson,BAL,13,DAL\n"
    ",Blank Rank,KC,5,MIA\n"
    "3,,DET,6,NYJ\n"
)


def test_rankings_csv_becomes_ranked_players():
    players = parse_rankings_csv(BOARD_CSV, "QB")
    assert [(p.positional_rank, p.name, p.team) for p in players] == [
        (1, "Josh Allen", "BUF"), (2, "Lamar Jackson", "BAL"),
    ]
    assert all(p.position == "QB" for p in players)


def test_a_rank_is_never_turned_into_projected_points():
    assert all(p.projected_points is None for p in parse_rankings_csv(BOARD_CSV, "QB"))


def test_dst_is_stored_under_sleepers_def_label():
    assert parse_rankings_csv(BOARD_CSV, "DST")[0].position == "DEF"


def test_a_changed_csv_layout_raises_rather_than_mis_reading_columns():
    with pytest.raises(FantasyGuruError, match="columns changed"):
        parse_rankings_csv("POS,NAME,TM\n1,Josh Allen,BUF\n", "QB")


def test_the_paywall_call_to_action_reads_as_locked():
    assert FantasyGuruSession._looks_locked("Jeff Mans: Weekly Rankings\nUNLOCK THE RANKINGS")


def test_a_membership_promo_on_an_unlocked_page_is_not_a_paywall():
    """Every page's promo says 'Already a member? Sign in' - logged in or not."""
    assert not FantasyGuruSession._looks_locked(
        "1 Josh Allen BUF\nNFL Memberships $99.99 Already a member? Sign in."
    )


def test_void_tags_in_head_do_not_swallow_the_page():
    """<link>/<meta> have no end tag; counting them as skipped blanked every page."""
    html = '<html><head><meta charset="utf-8"><link rel="x" href="/a.css"></head>' \
           '<body><link rel="preload" href="/b"><p>Priority Adds</p></body></html>'
    assert "Priority Adds" in html_to_text(html)


# --- weekly waiver report -----------------------------------------------------
REPORT = """
<html><head><link rel="stylesheet" href="/a.css"></head><body><main>
<h1>2026 Week 4 Waiver Report</h1>
<p>We'll go position-by-position identifying key waiver wire adds.</p>
<h2>Week 4 Waiver Targets</h2>
<h3>Quarterbacks</h3>
<p><strong>Jacoby Brissett, ARI</strong> (10% Owned Yahoo) — FAAB Recommendation: 0-2%</p>
<p>Brissett dropped back 54 times last week.</p>
<p>Just over 40% Owned – C.J. Stroud vs DAL</p>
<h3>Running Backs</h3>
<p><strong>Jaylen Wright, MIA</strong> (3% Owned Yahoo) — FAAB Recommendation: 5-10%</p>
<p><strong>Ollie Gordon, MIA</strong> (6% Owned Yahoo) — FAAB Recommendation: 3-7%</p>
<p>Miami will now turn to Ollie Gordon and Jaylen Wright.</p>
<h3>Wide Receivers</h3>
<p><strong>Kalif Raymond</strong> (10% Owned Yahoo) – FAAB Recommendation: 1-5%</p>
<p>Raymond has earned a look.</p>
<p>Names to Consider for Deeper Formats:</p>
<p><strong>Keenan Allen, IND</strong> (22% Owned Yahoo) – Coming off a 9-target game.</p>
<h3>Tight Ends</h3>
<p><strong>Kenyon Sadiq, NYJ</strong> (19% Owned Yahoo) — FAAB Recommendation: 15%</p>
<p>The Jets' injury report is a bloodbath.</p>
<h3>Defense / Special Teams</h3>
<p>Buffalo Bills vs NE – Maye threw some inexcusable picks.</p>
<h2>Favorite Priority Adds</h2>
<p>Kenyon Sadiq, TE, NYJ (15% FAAB)</p>
<p>Jaylen Wright, RB, MIA (5-10% FAAB)</p>
<p>If you have any specific add/drop questions, reach out.</p>
<h2>Related articles</h2>
<p><strong>Someone Else, KC</strong> (1% Owned Yahoo) — FAAB Recommendation: 99%</p>
</main></body></html>
"""


def _target(report, name):
    return next(t for t in report["targets"] if t["name"] == name)


def test_waiver_report_reads_title_week_and_priority_adds():
    r = parse_waiver_report(REPORT)
    assert (r["title"], r["week"]) == ("2026 Week 4 Waiver Report", 4)
    got = [(p["name"], p["position"], p["faab_pct_low"], p["faab_pct_high"]) for p in r["priority"]]
    assert got == [("Kenyon Sadiq", "TE", 15, 15), ("Jaylen Wright", "RB", 5, 10)]


def test_targets_carry_position_team_ownership_and_faab_range():
    t = _target(parse_waiver_report(REPORT), "Jacoby Brissett")
    assert (t["position"], t["team"], t["owned_pct"]) == ("QB", "ARI", 10)
    assert (t["faab_pct_low"], t["faab_pct_high"]) == (0, 2)
    assert t["blurb"].startswith("Brissett dropped back")


def test_a_single_faab_figure_is_both_ends_of_the_range():
    t = _target(parse_waiver_report(REPORT), "Kenyon Sadiq")
    assert (t["faab_pct_low"], t["faab_pct_high"]) == (15, 15)


def test_a_target_without_a_team_is_still_read():
    t = _target(parse_waiver_report(REPORT), "Kalif Raymond")
    assert t["team"] is None and t["position"] == "WR" and t["faab_pct_high"] == 5


def test_a_shared_write_up_belongs_to_every_pick_it_follows():
    r = parse_waiver_report(REPORT)
    assert _target(r, "Jaylen Wright")["blurb"] == _target(r, "Ollie Gordon")["blurb"]
    assert "Miami will now turn" in _target(r, "Jaylen Wright")["blurb"]


def test_deeper_format_names_have_no_faab_and_keep_their_inline_note():
    t = _target(parse_waiver_report(REPORT), "Keenan Allen")
    assert t["tier"] == "deeper" and t["faab_pct_low"] is None
    assert t["blurb"] == "Coming off a 9-target game."


def test_defense_streamers_are_read():
    d = parse_waiver_report(REPORT)["defenses"]
    assert [(x["name"], x["where"], x["opp"]) for x in d] == [("Buffalo Bills", "vs", "NE")]


def test_an_odd_line_is_surfaced_not_dropped():
    assert parse_waiver_report(REPORT)["unparsed"] == ["Just over 40% Owned – C.J. Stroud vs DAL"]


def test_related_articles_are_not_read_as_recommendations():
    assert "Someone Else" not in [t["name"] for t in parse_waiver_report(REPORT)["targets"]]


def test_a_report_with_no_recognisable_picks_raises():
    with pytest.raises(FantasyGuruError, match="layout may have changed"):
        parse_waiver_report("<main><h1>2026 Week 5 Waiver Report</h1><p>Prose only.</p></main>")


def test_the_newest_waiver_report_is_chosen_from_the_hub():
    hub = (
        '<a href="/2026-week-3-waiver-report">x</a>'
        '<a href="/2026-week-4-waiver-report">y</a>'
        '<a href="/2025-week-17-waiver-report">z</a>'
    )
    assert find_waiver_report_url(hub) == "https://www.fantasyguru.com/2026-week-4-waiver-report"
    assert find_waiver_report_url("<a href='/nfl-dfs'>") is None


# --- weekly projections ---------------------------------------------------------
PROJ_CSV = (
    "POS,PLAYER,TEAM,OPP,PPR,NON,HALF\n"
    "RB,Jahmyr Gibbs,DET,CAR,21.40,17.10,19.25\n"
    "WR,Bad Row,KC,LV,n/a,1,1\n"
)


def test_projections_keep_every_scoring_format():
    rows = parse_projections_csv(PROJ_CSV)
    assert rows[0] == {"position": "RB", "name": "Jahmyr Gibbs", "team": "DET", "opp": "CAR",
                       "ppr": 21.4, "half_ppr": 19.25, "std": 17.1}


def test_an_unreadable_projection_is_none_not_zero():
    assert parse_projections_csv(PROJ_CSV)[1]["ppr"] is None


def test_a_changed_projections_layout_raises():
    with pytest.raises(FantasyGuruError, match="columns changed"):
        parse_projections_csv("POS,PLAYER,TEAM,POINTS\nRB,X,DET,1\n")
