"""Sleeper read connector.

Sleeper's read API is public and unauthenticated, which makes it the cheapest
platform to build against — no cookies, no OAuth. Writes (lineups, waiver
claims) are a different story: they go through an authenticated GraphQL
endpoint and land in Stage 4.

Two details worth knowing before reading the code:

* ``/v1/players/nfl`` is a ~5MB document of every NFL player, and Sleeper asks
  that it be fetched at most once a day. It is cached to disk with a long TTL;
  the 3am waiver job must not be re-downloading it.
* The ``starters`` array is **positional**. Index *i* corresponds to the *i*-th
  non-bench entry in the league's ``roster_positions``. Getting that alignment
  wrong silently mislabels which slot a player occupies, which would make the
  lineup guardian propose illegal swaps — so :func:`build_slots` is tested
  directly.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from fcc.core.config import get_settings
from fcc.engines.value import RankedPlayer
from fcc.platforms.base import (
    LeagueSummary,
    Matchup,
    Player,
    Roster,
    RosterSlot,
    normalize_status,
)

log = logging.getLogger(__name__)

BASE = "https://api.sleeper.app/v1"
#: A different host and payload shape from everything else this client reads,
#: so it is not routed through :meth:`SleeperClient._get`.
PROJECTIONS_BASE = "https://api.sleeper.app/projections/nfl"

#: The web app's own GraphQL endpoint. Unlike everything above it needs the
#: user's token, because it can see things the public API can't - notably
#: waiver claims that haven't cleared yet.
GRAPHQL_URL = "https://sleeper.com/graphql"

#: Claim statuses actually observed from the live API. Anything else is
#: reported verbatim and treated as still pending rather than guessed at -
#: the pending spelling itself has not been seen yet.
SETTLED_CLAIM_STATUSES = {"complete", "failed"}

#: Slots that are not part of the active lineup.
NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}

#: Sleeper's sentinel for an intentionally empty starting slot.
EMPTY_SLOT_IDS = {"0", "", None}

#: The player index changes slowly and is large; Sleeper asks for once-a-day.
PLAYER_CACHE_TTL = 60 * 60 * 20


class SleeperError(RuntimeError):
    pass


def build_slots(
    roster_positions: list[str],
    starter_ids: list[str],
    all_player_ids: list[str],
    lookup,
) -> list[RosterSlot]:
    """Assemble a roster into labelled slots.

    ``starter_ids`` aligns positionally with the non-bench entries of
    ``roster_positions``; everyone else on the roster goes to the bench. Kept as
    a free function so the alignment can be tested without any HTTP.
    """
    slots: list[RosterSlot] = []
    starting_positions = [p for p in roster_positions if p not in NON_STARTING_SLOTS]

    for index, slot_name in enumerate(starting_positions):
        pid = starter_ids[index] if index < len(starter_ids) else None
        player = None if pid in EMPTY_SLOT_IDS else lookup(pid)
        slots.append(RosterSlot(slot=slot_name, player=player, starter=True))

    started = {p for p in starter_ids if p not in EMPTY_SLOT_IDS}
    for pid in all_player_ids:
        if pid in started or pid in EMPTY_SLOT_IDS:
            continue
        slots.append(RosterSlot(slot="BN", player=lookup(pid), starter=False))

    return slots


class SleeperClient:
    """Read-only Sleeper access with an on-disk player index."""

    platform = "sleeper"

    def __init__(
        self,
        league_id: str,
        league_key: str = "sleeper",
        user_id: str | None = None,
        client: httpx.Client | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.league_id = league_id
        self.league_key = league_key
        self.user_id = user_id
        self._client = client or httpx.Client(
            timeout=30,
            headers={"user-agent": "fantasy-command-center/0.1 (personal use)"},
        )
        self.cache_dir = cache_dir or (get_settings().data_path / "cache" / "sleeper")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._players: dict[str, dict] | None = None
        self._league: dict | None = None
        self._transactions_cache: list[dict] | None = None

    # -- transport --------------------------------------------------------
    def _get(self, path: str) -> Any:
        resp = self._client.get(f"{BASE}/{path.lstrip('/')}")
        if resp.status_code == 404:
            raise SleeperError(f"Sleeper returned 404 for {path} — check the league id.")
        resp.raise_for_status()
        return resp.json()

    # -- player index -----------------------------------------------------
    def players(self, force: bool = False) -> dict[str, dict]:
        """The NFL player index, cached to disk.

        Sleeper asks that this be fetched at most once a day; it is several
        megabytes. Everything else here is small enough to fetch per call.
        """
        if self._players is not None and not force:
            return self._players

        path = self.cache_dir / "players_nfl.json"
        if not force and path.exists() and time.time() - path.stat().st_mtime < PLAYER_CACHE_TTL:
            self._players = json.loads(path.read_text())
            return self._players

        log.info("fetching Sleeper player index (large, cached for ~20h)")
        data = self._get("players/nfl")
        path.write_text(json.dumps(data))
        self._players = data
        return data

    def player(self, player_id: str | None) -> Player | None:
        if player_id in EMPTY_SLOT_IDS:
            return None
        raw = self.players().get(str(player_id))
        if raw is None:
            # An id the index doesn't know is not a healthy player - say so
            # rather than inventing an ACTIVE placeholder the guardian trusts.
            return Player(
                player_id=str(player_id),
                name=f"Unknown player {player_id}",
                status=normalize_status("unknown-sentinel"),
            )
        # Team defences carry no first/last name.
        name = raw.get("full_name") or " ".join(
            filter(None, [raw.get("first_name"), raw.get("last_name")])
        ) or raw.get("team") or str(player_id)
        return Player(
            player_id=str(player_id),
            name=name,
            position=raw.get("position"),
            # Sleeper reports every slot a player qualifies for here; without it
            # a dual-eligible RB/WR looks ineligible for half the flex slots.
            positions=raw.get("fantasy_positions") or [],
            team=raw.get("team"),
            status=normalize_status(raw.get("injury_status")),
            status_detail=raw.get("injury_status") or "",
        )

    # -- league state -----------------------------------------------------
    def league(self) -> dict:
        if self._league is None:
            self._league = self._get(f"league/{self.league_id}")
        return self._league

    def state(self) -> dict:
        return self._get("state/nfl")

    def current_week(self) -> int | None:
        try:
            return int(self.state().get("week"))
        except (TypeError, ValueError):
            return None

    def _my_roster_raw(self) -> dict:
        rosters = self._get(f"league/{self.league_id}/rosters")
        if not rosters:
            raise SleeperError(f"League {self.league_id} returned no rosters.")
        if self.user_id:
            for r in rosters:
                if str(r.get("owner_id")) == str(self.user_id):
                    return r
            raise SleeperError(
                f"No roster in league {self.league_id} is owned by user {self.user_id}. "
                "Check sleeper_user_id."
            )
        if len(rosters) == 1:
            return rosters[0]
        raise SleeperError(
            "This league has multiple rosters and no user id is configured; "
            "set team_id on the league in config/leagues.yml."
        )

    # -- ReadConnector ----------------------------------------------------
    def roster(self, week: int | None = None) -> Roster:
        raw = self._my_roster_raw()
        league = self.league()
        slots = build_slots(
            league.get("roster_positions") or [],
            raw.get("starters") or [],
            raw.get("players") or [],
            self.player,
        )
        return Roster(
            league_key=self.league_key,
            team_id=str(raw.get("roster_id", "")),
            team_name=self._team_name(raw),
            week=week or self.current_week(),
            slots=slots,
        )

    def _team_name(self, roster_raw: dict) -> str:
        owner = str(roster_raw.get("owner_id") or "")
        for user in self._get(f"league/{self.league_id}/users") or []:
            if str(user.get("user_id")) == owner:
                meta = user.get("metadata") or {}
                return meta.get("team_name") or user.get("display_name") or ""
        return ""

    def league_summary(self) -> LeagueSummary:
        league = self.league()
        raw = self._my_roster_raw()
        settings = raw.get("settings") or {}
        budget = (league.get("settings") or {}).get("waiver_budget")
        used = settings.get("waiver_budget_used")
        return LeagueSummary(
            key=self.league_key,
            platform=self.platform,
            name=league.get("name") or "",
            season=int(league["season"]) if str(league.get("season", "")).isdigit() else None,
            week=self.current_week(),
            team_name=self._team_name(raw),
            wins=settings.get("wins"),
            losses=settings.get("losses"),
            ties=settings.get("ties"),
            faab_remaining=(budget - used) if budget is not None and used is not None else None,
            waiver_position=settings.get("waiver_position"),
        )

    # -- league-wide budget state -----------------------------------------
    def league_budgets(self) -> list[dict]:
        """Every roster's remaining FAAB, and whether it still has a team.

        In a guillotine league this is the single most decision-relevant fact
        available: if you hold more FAAB than everyone else combined, nobody
        can outbid you and the only question left is what the player is worth.

        A chopped roster is inferred from having no players, because Sleeper
        exposes no "eliminated" flag. That is an inference, not a fact the API
        states, and callers should present it as such.
        """
        budget = (self.league().get("settings") or {}).get("waiver_budget")
        rosters = self._get(f"league/{self.league_id}/rosters") or []
        owners = {}
        for user in self._get(f"league/{self.league_id}/users") or []:
            meta = user.get("metadata") or {}
            owners[str(user.get("user_id"))] = (
                meta.get("team_name") or user.get("display_name") or ""
            )

        out: list[dict] = []
        for roster in rosters:
            settings = roster.get("settings") or {}
            used = settings.get("waiver_budget_used")
            players = roster.get("players") or []
            out.append(
                {
                    "roster_id": roster.get("roster_id"),
                    "owner_id": roster.get("owner_id"),
                    "team_name": owners.get(str(roster.get("owner_id")), ""),
                    "budget_used": used,
                    "budget_remaining": (
                        budget - used if budget is not None and used is not None else None
                    ),
                    "player_count": len(players),
                    # Inference, not an API fact: Sleeper has no eliminated flag.
                    "likely_chopped": len(players) == 0,
                    "is_me": str(roster.get("owner_id")) == str(self.user_id or ""),
                }
            )
        return out

    # -- free agents and projections ---------------------------------------
    def rostered_player_ids(self) -> set[str]:
        """Every player id owned by any roster in the league, mine included."""
        rosters = self._get(f"league/{self.league_id}/rosters") or []
        out: set[str] = set()
        for roster in rosters:
            out.update(str(pid) for pid in (roster.get("players") or []))
        return out

    def projections(self, week: int) -> dict[str, dict]:
        """Sleeper's own weekly player projections, sourced from RotoWire.

        Undocumented but public - no auth, no cookie. Not disk-cached like the
        player index: projections move through the week as injury news lands,
        so a stale copy is actively misleading rather than merely old.
        """
        season = self.league().get("season")
        resp = self._client.get(
            f"{PROJECTIONS_BASE}/{season}/{week}", params={"season_type": "regular"}
        )
        resp.raise_for_status()
        return {str(row["player_id"]): row for row in resp.json() if row.get("player_id")}

    def ranked_players(self, week: int | None = None) -> list[RankedPlayer]:
        """Every projected player this week, scored to match this league's format.

        Feeds :mod:`fcc.engines.value`'s replacement-level math, which expects
        the *whole* ranked universe at a position rather than free agents
        only - replacement level is defined as the first player past everyone
        already rostered, so the pool has to include the rostered players too.
        """
        week = week or self.current_week() or 1
        rec = (self.league().get("scoring_settings") or {}).get("rec") or 0
        field = "pts_ppr" if rec >= 1 else "pts_half_ppr" if rec >= 0.5 else "pts_std"

        out: list[RankedPlayer] = []
        for pid, row in self.projections(week).items():
            player = row.get("player") or {}
            position = player.get("position")
            if not position:
                continue
            name = (
                " ".join(filter(None, [player.get("first_name"), player.get("last_name")]))
                or player.get("team")
                or pid
            )
            stats = row.get("stats") or {}
            out.append(
                RankedPlayer(
                    player_id=pid,
                    name=name,
                    position=position.upper(),
                    projected_points=stats.get(field),
                    team=player.get("team"),
                )
            )
        return out

    # -- authenticated reads ------------------------------------------------
    def waiver_claims(self, token: str) -> list[dict]:
        """Every waiver claim on my roster this season, pending ones included.

        The public API only reports a claim after the Wednesday clear. This
        uses the same GraphQL query the Sleeper web app does, captured from
        DevTools; the token is passed in rather than read here so the rest of
        this client stays credential-free.
        """
        league_id = str(self.league_id)
        # Interpolated into the query text (that is how the web app sends it),
        # so refuse anything that isn't plainly an id.
        if not league_id.isdigit():
            raise SleeperError(f"League id {league_id!r} is not numeric; refusing to query.")
        roster_id = int(self._my_roster_raw()["roster_id"])

        query = (
            "query league_transactions_filtered { league_transactions_filtered("
            f'league_id: "{league_id}", roster_id_filters: [{roster_id}], '
            'type_filters: ["waiver"], leg_filters: [], status_filters: []) '
            "{ adds drops leg settings status created status_updated transaction_id } }"
        )
        resp = self._client.post(
            GRAPHQL_URL,
            headers={"authorization": token, "content-type": "application/json"},
            json={"operationName": "league_transactions_filtered", "variables": {},
                  "query": query},
        )
        if resp.status_code in (401, 403):
            raise SleeperError(
                "Sleeper rejected the token - it has probably expired. Capture a "
                "fresh one and `fcc secrets set sleeper_token`."
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise SleeperError(f"Sleeper GraphQL error: {body['errors']}")
        return (body.get("data") or {}).get("league_transactions_filtered") or []

    # -- transactions -----------------------------------------------------
    def transactions(self, week: int) -> list[dict]:
        """Every transaction reported for a week, including failed waiver bids.

        Sleeper reports losing claims alongside the winning one, which is what
        makes it possible to see how contested a player actually was rather
        than only what the winner paid.
        """
        return self._get(f"league/{self.league_id}/transactions/{week}") or []

    def transaction_history(self, through_week: int | None = None) -> list[dict]:
        """Transactions for every week so far this season.

        One request per week is unavoidable — Sleeper has no bulk endpoint —
        so results are cached on the instance for the life of the client.
        """
        last = through_week or self.current_week() or 1
        if self._transactions_cache is not None:
            return self._transactions_cache
        out: list[dict] = []
        for week in range(1, last + 1):
            try:
                out.extend(self.transactions(week))
            except Exception as exc:  # noqa: BLE001 - a missing week is not fatal
                log.warning("could not read week %s transactions: %s", week, exc)
        self._transactions_cache = out
        return out

    def matchup(self, week: int | None = None) -> Matchup | None:
        week = week or self.current_week()
        if week is None:
            return None
        rows = self._get(f"league/{self.league_id}/matchups/{week}") or []
        mine = self._my_roster_raw()
        my_roster_id = mine.get("roster_id")

        my_row = next((r for r in rows if r.get("roster_id") == my_roster_id), None)
        if my_row is None:
            return None
        opponent = next(
            (
                r
                for r in rows
                if r.get("matchup_id") == my_row.get("matchup_id")
                and r.get("roster_id") != my_roster_id
            ),
            None,
        )
        return Matchup(
            week=week,
            opponent_name="",  # filled by the caller if it wants a name lookup
            points_for=my_row.get("points"),
            points_against=opponent.get("points") if opponent else None,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SleeperClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
