"""Connector selection and failure isolation.

The dashboard aggregates platforms that fail independently. The property under
test is that one unreachable league produces one bad row — never an exception
that blanks every other league on the page.
"""

import pytest

from fcc.core.config import LeagueConfig
from fcc.platforms.registry import (
    UnsupportedPlatform,
    connector_for,
    load_summaries,
)
from fcc.platforms.sleeper import SleeperClient


def league(key="sleeper_main", platform="sleeper", **kw):
    return LeagueConfig(
        key=key, platform=platform, league_id=kw.pop("league_id", "L1"), season=2026, **kw
    )


def test_sleeper_league_builds_a_sleeper_client():
    connector = connector_for(league(team_id="user-me"))
    assert isinstance(connector, SleeperClient)
    assert connector.user_id == "user-me"
    assert connector.league_key == "sleeper_main"


@pytest.mark.parametrize("platform", ["yahoo", "espn"])
def test_platforms_not_built_yet_say_so_clearly(platform):
    with pytest.raises(UnsupportedPlatform, match="Stage 3"):
        connector_for(league(platform=platform))


def test_pickem_is_not_a_roster_platform():
    with pytest.raises(UnsupportedPlatform, match="Pick'em"):
        connector_for(league(platform="espn_pickem"))


def test_unknown_platform_is_rejected_at_config_load():
    """A typo in leagues.yml fails when the config is read, not at 3am.

    The Platform literal catches it before the registry is ever consulted, so
    the registry's own "no connector" branch is a defensive backstop for
    programmatic callers rather than the path a bad config takes.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        league(platform="fleaflicker")


def test_one_failing_league_does_not_stop_the_others(monkeypatch):
    from fcc.platforms.base import LeagueSummary

    good = league(key="ok")
    bad = league(key="broken")

    def fake_connector(lg, store=None):
        class C:
            def league_summary(self):
                if lg.key == "broken":
                    raise RuntimeError("401 Unauthorized")
                return LeagueSummary(key=lg.key, platform="sleeper", wins=7, losses=3)

        return C()

    monkeypatch.setattr("fcc.platforms.registry.connector_for", fake_connector)
    results = {r.key: r for r in load_summaries([good, bad])}

    assert results["ok"].ok
    assert results["ok"].summary.record == "7-3"
    assert not results["broken"].ok
    assert "401 Unauthorized" in results["broken"].error
    # Broken, but still a *supported* platform - the dashboard shows those
    # differently from ones that aren't built yet.
    assert results["broken"].supported


def test_unsupported_platform_is_marked_unsupported_not_errored():
    results = {r.key: r for r in load_summaries([league(key="y", platform="yahoo")])}
    assert results["y"].supported is False
    assert results["y"].summary is None


def test_disabled_leagues_are_skipped():
    results = load_summaries([league(key="off", platform="yahoo", enabled=False)])
    assert results == []
