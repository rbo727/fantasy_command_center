"""The `fcc pickem dump` command.

Exercises the real command through typer's runner with a stubbed ESPN client,
so the redaction wiring is verified rather than assumed — the whole point of the
command is that its output is safe to hand to someone else.
"""

import json

import pytest
from typer.testing import CliRunner

from fcc.cli import app

RESPONSE = {
    "entryId": "9f8e7d6c",
    "userId": "{2A4B6C8D-1E3F-4A5B-8C7D-9E0F1A2B3C4D}",
    "displayName": "Ryan O.",
    "propositions": [
        {
            "id": "401671801",
            "possibleOutcomes": [
                {"id": "9001", "abbrev": "BAL", "name": "Baltimore Ravens"},
                {"id": "9002", "abbrev": "KC", "name": "Kansas City Chiefs"},
            ],
        }
    ],
    "group": {"groupId": 55512345, "name": "Work League"},
}


class StubClient:
    challenge = "nfl-pickem-2026"

    def challenge_info(self, week=None):
        return RESPONSE

    def entry(self):
        return RESPONSE

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def stub_league(monkeypatch, tmp_path):
    from fcc.core.config import LeagueConfig

    league = LeagueConfig(
        key="espn_pickem",
        platform="espn_pickem",
        league_id="nfl-pickem-2026",
        season=2026,
    )
    monkeypatch.setattr("fcc.cli._pickem_client", lambda key: (StubClient(), league))
    return league


def test_dump_is_redacted_by_default(stub_league, tmp_path):
    out = tmp_path / "dump.json"
    result = CliRunner().invoke(app, ["pickem", "dump", "espn_pickem", "--out", str(out)])

    assert result.exit_code == 0, result.output
    blob = out.read_text()
    for secret in ("2A4B6C8D", "Ryan O.", "Work League", "9f8e7d6c", "55512345"):
        assert secret not in blob, f"{secret!r} leaked into a shareable dump"

    doc = json.loads(blob)
    outcomes = doc["challenge_info"]["propositions"][0]["possibleOutcomes"]
    assert [o["abbrev"] for o in outcomes] == ["BAL", "KC"]
    assert "_fcc_redaction" in doc


def test_dump_reports_what_it_removed(stub_league, tmp_path):
    result = CliRunner().invoke(
        app, ["pickem", "dump", "espn_pickem", "--out", str(tmp_path / "d.json")]
    )
    assert "Redacted" in result.output
    assert "displayName" in result.output
    # The terminal summary must not echo the values it removed.
    assert "Work League" not in result.output


def test_raw_dump_is_possible_but_warns_loudly(stub_league, tmp_path):
    out = tmp_path / "raw.json"
    result = CliRunner().invoke(
        app, ["pickem", "dump", "espn_pickem", "--raw", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Work League" in out.read_text()
    assert "UNREDACTED" in result.output


def test_dump_keeps_the_ids_the_fixture_exists_to_show(stub_league, tmp_path):
    """The wrapper key must not poison what's under it.

    `entry` and `challenge_info` wrap the dump, and an ancestor-matching
    subtree check treated everything beneath them as personal — stripping the
    proposition and outcome id formats that are the whole reason for capturing
    a fixture.
    """
    out = tmp_path / "dump.json"
    CliRunner().invoke(app, ["pickem", "dump", "espn_pickem", "--out", str(out)])
    doc = json.loads(out.read_text())

    prop = doc["entry"]["propositions"][0]
    assert prop["id"] == "401671801"
    assert [o["id"] for o in prop["possibleOutcomes"]] == ["9001", "9002"]
    # ...while the personal ids in the same document still go.
    assert doc["entry"]["group"]["name"] != "Work League"
    assert "9f8e7d6c" not in json.dumps(doc)
