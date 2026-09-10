"""Builds the right connector for a configured league.

The dashboard aggregates across platforms that fail independently: ESPN cookies
expire, Sleeper has an outage, Yahoo isn't wired up yet. One unreachable league
must never blank the whole page, so :func:`load_summaries` returns a result
per league — each either data or an error — rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fcc.core.config import LeagueConfig, get_settings
from fcc.core.secrets import Keys, SecretStore
from fcc.platforms.base import LeagueSummary, ReadConnector

log = logging.getLogger(__name__)


class UnsupportedPlatform(RuntimeError):
    """A platform that has no read connector yet."""


def connector_for(league: LeagueConfig, store: SecretStore | None = None) -> ReadConnector:
    store = store or SecretStore()

    if league.platform == "sleeper":
        from fcc.platforms.sleeper import SleeperClient

        return SleeperClient(
            league_id=league.league_id,
            league_key=league.key,
            user_id=league.team_id or store.get(Keys.SLEEPER_USER_ID),
        )

    if league.platform in ("yahoo", "espn"):
        raise UnsupportedPlatform(
            f"{league.platform} reads land in Stage 3. The league is configured but "
            "not yet readable."
        )

    if league.platform == "espn_pickem":
        raise UnsupportedPlatform(
            "Pick'em entries aren't rosters; they appear on the Pick'em tab, not here."
        )

    raise UnsupportedPlatform(f"No connector for platform {league.platform!r}.")


@dataclass
class LeagueResult:
    """A league's data, or why it isn't available. Never both, never neither."""

    key: str
    platform: str
    summary: LeagueSummary | None = None
    error: str | None = None
    supported: bool = True

    @property
    def ok(self) -> bool:
        return self.summary is not None


def load_summaries(leagues: list[LeagueConfig] | None = None) -> list[LeagueResult]:
    """Fetch every configured league, isolating failures to their own row."""
    leagues = leagues if leagues is not None else get_settings().leagues()
    results: list[LeagueResult] = []

    for league in leagues:
        if not league.enabled:
            continue
        result = LeagueResult(key=league.key, platform=league.platform)
        try:
            connector = connector_for(league)
            result.summary = connector.league_summary()
        except UnsupportedPlatform as exc:
            result.supported = False
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad league must not blank the page
            log.warning("league %s failed to load: %s", league.key, exc)
            result.error = f"{type(exc).__name__}: {exc}"
        results.append(result)

    return results
