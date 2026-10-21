"""FastAPI backend for the dashboard.

Single-user by design. It holds write access to four accounts, so it binds to
localhost by default and is meant to be reached over an SSH tunnel or from
behind the seedbox's own reverse proxy — never exposed directly.

Endpoints are shaped around the question the dashboard actually asks: *what
needs my attention?* The action queue is the point; the league tables are
context.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from fcc.core.actions import ActionGate
from fcc.core.config import get_settings
from fcc.core.db import init_db, session_scope
from fcc.core.models import Action, ActionStatus
from fcc.platforms.registry import connector_for, load_summaries

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Fantasy Command Center", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------
def _action_json(action: Action) -> dict[str, Any]:
    return {
        "id": action.id,
        "kind": action.kind.value,
        "tier": action.tier.value,
        "status": action.status.value,
        "league_id": action.league_id,
        "season": action.season,
        "week": action.week,
        "payload": action.payload,
        "rationale": action.rationale,
        "error": action.error,
        "verification": action.verification,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "submitted_at": action.submitted_at.isoformat() if action.submitted_at else None,
        "needs_approval": action.status is ActionStatus.PROPOSED,
        "is_terminal": action.is_terminal,
    }


class Decision(BaseModel):
    reason: str = ""


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "dry_run": settings.dry_run,
        "timezone": settings.timezone,
        "leagues_configured": len(settings.leagues()),
    }


@app.get("/api/leagues")
def leagues() -> list[dict]:
    """Every configured league. A failing league reports its error inline.

    Deliberately never raises: an expired ESPN cookie should grey out one card,
    not blank the dashboard.
    """
    out = []
    for result in load_summaries():
        row: dict[str, Any] = {
            "key": result.key,
            "platform": result.platform,
            "supported": result.supported,
            "error": result.error,
        }
        if result.summary:
            s = result.summary
            row |= {
                "name": s.name,
                "season": s.season,
                "week": s.week,
                "team_name": s.team_name,
                "record": s.record,
                "rank": s.rank,
                "faab_remaining": s.faab_remaining,
                "waiver_position": s.waiver_position,
            }
        out.append(row)
    return out


@app.get("/api/leagues/{key}/roster")
def roster(key: str, week: int | None = None) -> dict:
    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc

    try:
        data = connector_for(league).roster(week=week)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    return {
        "league_key": data.league_key,
        "team_name": data.team_name,
        "week": data.week,
        "slots": [
            {
                "slot": slot.slot,
                "starter": slot.starter,
                "player": None
                if slot.player is None
                else {
                    "id": slot.player.player_id,
                    "name": slot.player.name,
                    "position": slot.player.position,
                    "team": slot.player.team,
                    "status": slot.player.status.value,
                    "status_detail": slot.player.status_detail,
                    "available": slot.player.available,
                },
            }
            for slot in data.slots
        ],
        "problems": [
            {"slot": s.slot, "player": s.player.name, "status": s.player.status.value}
            for s in data.problem_starters()
        ],
        "warnings": [
            {"slot": s.slot, "player": s.player.name, "status": s.player.status.value}
            for s in data.questionable_starters()
        ],
    }


@app.get("/api/leagues/{key}/lineup")
def lineup(key: str, week: int | None = None) -> dict:
    """What the guardian would change, and what it refuses to decide alone."""
    from fcc.engines.lineup import plan_lineup

    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc

    try:
        data = connector_for(league).roster(week=week)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    plan = plan_lineup(data, week=week)
    return {
        "league_key": key,
        "week": data.week,
        "clean": plan.clean,
        "swaps": [
            {
                "slot": s.slot,
                "out": s.out_player.name,
                "out_status": s.out_player.status.value,
                "in": s.in_player.name,
                "reason": s.reason,
            }
            for s in plan.swaps
        ],
        "warnings": [
            {"kind": w.kind, "detail": w.detail, "slot": w.slot, "player": w.player}
            for w in plan.warnings
        ],
    }


@app.get("/api/leagues/{key}/faab")
def faab(key: str, position: str | None = None) -> dict:
    """The waiver market: what winning has cost, and who can still outbid you.

    Everything `fcc faab` prints, as JSON. The guillotine block is present only
    for leagues declared that format, since its advice is wrong elsewhere.
    """
    from fcc.engines.guillotine import from_budget_rows
    from fcc.engines.market import BidMarket, parse_claims

    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc
    if league.platform != "sleeper":
        raise HTTPException(
            400, f"{key} is a {league.platform} league; FAAB history is Sleeper-only so far."
        )

    try:
        client = connector_for(league)
        budget_total = (client.league().get("settings") or {}).get("waiver_budget") or 0
        summary = client.league_summary()
        claims = parse_claims(client.transaction_history(), client.players())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    remaining = summary.faab_remaining if summary.faab_remaining is not None else budget_total
    market = BidMarket(claims=claims)
    market = market.comparable(position) if position else market

    ladder = []
    for prob in (0.5, 0.65, 0.8, 0.9, 1.0):
        price = market.price_for_win_probability(prob, budget_total)
        if price is not None:
            ladder.append(
                {
                    "probability": prob,
                    "price": price,
                    "over_budget": price > (remaining or 0),
                    "share_of_remaining": (price / remaining) if remaining else None,
                }
            )

    payload: dict[str, Any] = {
        "league_key": key,
        "format": league.format,
        "week": summary.week,
        "budget": {"total": budget_total, "remaining": remaining},
        "market": market.summary(),
        "ladder": ladder,
        "top_claims": [
            {
                "week": c.week,
                "player": c.player_name,
                "position": c.position,
                "winning_bid": c.winning_bid,
                "runner_up": c.runner_up,
                "bidders": c.bidder_count,
            }
            for c in sorted(
                (c for c in market.claims if c.winning_bid is not None),
                key=lambda c: c.winning_bid,
                reverse=True,
            )[:12]
        ],
        "guillotine": None,
    }

    if league.format == "guillotine":
        try:
            rows = client.league_budgets()
            state = from_budget_rows(rows, week=summary.week)
            payload["guillotine"] = {
                "teams_alive": state.teams_alive,
                "my_remaining": state.my_remaining,
                "max_rival_budget": state.max_rival_budget,
                "can_outbid_anyone": state.can_outbid_anyone,
                "price_to_guarantee": state.price_to_guarantee,
                "my_share": state.my_share,
                "spend_cap": state.spend_cap(),
                "advice": state.advice(),
                "rosters": sorted(
                    rows, key=lambda r: (r.get("budget_remaining") or 0), reverse=True
                ),
            }
        except Exception as exc:  # noqa: BLE001 - advisory, never fatal
            log.warning("guillotine state unavailable for %s: %s", key, exc)
            payload["guillotine"] = {"error": f"{type(exc).__name__}: {exc}"}

    payload["free_agents"] = _free_agent_board(client, week=summary.week, budget_total=budget_total,
                                                 remaining=remaining, claims=claims)
    return payload


@app.get("/api/leagues/{key}/targets")
def targets(key: str, position: str | None = None, limit: int = 12) -> dict:
    """Your lineup's holes this week, and who on the wire fills them.

    Distinct from /waiver-report, which is FantasyGuru's board filtered to
    availability. This one starts from *your* roster: which starting slots have
    nobody usable in them, how long each hole lasts, and which free agents beat
    the next free alternative at that position.
    """
    from fcc.engines.market import BidMarket, parse_claims
    from fcc.engines.targets import find_targets, rental_warning

    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc
    if league.platform != "sleeper":
        raise HTTPException(400, f"{key} is a {league.platform} league; this is Sleeper-only.")

    try:
        client = connector_for(league)
        week = client.current_week() or 1
        roster = client.roster(week=week)
        universe = client.ranked_players(week=week)
        rostered = client.rostered_player_ids()
        summary = client.league_summary()
        budget_total = (client.league().get("settings") or {}).get("waiver_budget") or 0
        budgets = client.league_budgets()
        claims = parse_claims(client.transaction_history(), client.players())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    remaining = summary.faab_remaining if summary.faab_remaining is not None else budget_total
    available = [p for p in universe if getattr(p, "player_id", None) not in rostered]
    teams = len([r for r in budgets if not r.get("likely_chopped")]) or 12

    found, needs = find_targets(
        available, universe, roster, teams=teams, week=week,
        positions=[position] if position else None, limit=limit,
    )
    full_market = BidMarket(claims=claims)

    rows = []
    for t in found:
        market = full_market.comparable(t.player.pos)
        typical = market.price_for_win_probability(0.65, budget_total)
        rows.append(
            {
                "name": t.player.name,
                "position": t.player.pos,
                "team": getattr(t.player, "team", None),
                "projected": t.player.projected_points,
                "value_over_free": t.vor,
                "beats_current": t.beats_current,
                "fills": None
                if t.fills is None
                else {
                    "slot": t.fills.slot,
                    "cause": t.fills.cause,
                    "incumbent": t.fills.incumbent,
                    "weeks": t.fills.weeks,
                    "is_rental": t.fills.is_rental,
                },
                "typical_bid": typical,
                "rental_warning": (
                    rental_warning(t, typical, remaining) if typical is not None else None
                ),
                "reasons": t.reasons,
            }
        )

    return {
        "league_key": key,
        "week": week,
        "teams_alive": teams,
        "budget": {"total": budget_total, "remaining": remaining},
        "available_count": len(available),
        "needs": [
            {
                "slot": n.slot,
                "position": n.position,
                "cause": n.cause,
                "incumbent": n.incumbent,
                "weeks": n.weeks,
                "is_rental": n.is_rental,
            }
            for n in needs
        ],
        "targets": rows,
    }


@app.get("/api/leagues/{key}/waiver-claims")
def waiver_claims(key: str) -> dict:
    """My waiver claims: what's queued for the next clear, and how the last one went.

    Read-only. Needs the Sleeper token because pending claims are private;
    without it this says so rather than showing an empty queue that looks real.
    """
    from fcc.core.secrets import Keys, SecretStore
    from fcc.platforms.sleeper import SETTLED_CLAIM_STATUSES

    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc
    if league.platform != "sleeper":
        raise HTTPException(400, f"{key} is a {league.platform} league; claims are Sleeper-only.")

    token = SecretStore().get(Keys.SLEEPER_TOKEN)
    if not token:
        return {"configured": False, "pending": [], "last_clear": None}

    try:
        client = connector_for(league)
        raw = client.waiver_claims(token)
        players = client.players()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    def who(pids) -> list[dict]:
        out = []
        for pid in pids or {}:
            p = players.get(str(pid)) or {}
            out.append({
                "id": str(pid),
                "name": p.get("full_name") or p.get("team") or str(pid),
                "position": p.get("position"),
                "team": p.get("team"),
            })
        return out

    claims = []
    for t in raw:
        s = t.get("settings") or {}
        claims.append({
            "status": t.get("status"),
            "leg": t.get("leg"),
            "bid": s.get("waiver_bid"),
            "seq": s.get("seq"),
            "adds": who(t.get("adds")),
            "drops": who(t.get("drops")),
            "updated": t.get("status_updated") or t.get("created"),
        })

    pending = sorted(
        (c for c in claims if c["status"] not in SETTLED_CLAIM_STATUSES),
        key=lambda c: (c["seq"] is None, c["seq"] or 0),
    )
    settled = [c for c in claims if c["status"] in SETTLED_CLAIM_STATUSES]
    last_clear = None
    if settled:
        leg = max(c["leg"] or 0 for c in settled)
        last_clear = {
            "leg": leg,
            "claims": sorted(
                (c for c in settled if (c["leg"] or 0) == leg),
                key=lambda c: (c["status"] != "complete", -(c["bid"] or 0)),
            ),
        }
    return {"configured": True, "pending": pending, "last_clear": last_clear}


@app.get("/api/leagues/{key}/waiver-report")
def waiver_report(key: str) -> dict:
    """FantasyGuru's weekly waiver report, priced and checked against this league.

    Read from the cache `fcc fantasyguru refresh` writes. FAAB percentages are
    the author's, for 12-team leagues, of the *starting* budget; they are
    converted to dollars at this league's budget but not rescaled for league
    size - that adjustment is a judgement call left to you.
    """
    from fcc.engines.value import RankedPlayer, normalize_player_name, rank_lookup
    from fcc.sources.fantasyguru import load_waiver_report

    settings = get_settings()
    try:
        league = settings.league(key)
    except KeyError as exc:
        raise HTTPException(404, f"No league configured with key {key!r}") from exc
    if league.platform != "sleeper":
        raise HTTPException(400, f"{key} is a {league.platform} league; this is Sleeper-only.")

    report = load_waiver_report()
    if report is None:
        return {"available": False}

    try:
        client = connector_for(league)
        budget_total = (client.league().get("settings") or {}).get("waiver_budget") or 0
        players = client.players()
        rostered = client.rostered_player_ids()
        mine = {
            s.player.player_id for s in client.roster().slots if s.player is not None
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    items = report["priority"] + report["targets"]
    # Index the report the way rank_lookup expects, one entry per distinct
    # player (priority adds repeat names from the position lists, and a
    # duplicate would read as ambiguous), then run every Sleeper player through it.
    board: dict[str, list[dict]] = {}
    ids: dict[tuple, int] = {}
    item_ids: list[int | None] = []
    for item in items:
        if not item.get("position"):
            item_ids.append(None)
            continue
        k = (item["position"], normalize_player_name(item["name"]), item.get("team"))
        if k not in ids:
            ids[k] = len(ids)
            board.setdefault(item["position"], []).append(
                {"name": item["name"], "team": item.get("team"), "rank": ids[k]}
            )
        item_ids.append(ids[k])
    lookup = rank_lookup(board)
    hits: dict[int, list[str]] = {}
    for pid, raw in players.items():
        if not raw.get("position") or not (raw.get("full_name") or raw.get("team")):
            continue
        idx = lookup(RankedPlayer(
            name=raw.get("full_name") or raw.get("team"), position=raw["position"],
            team=raw.get("team"),
        ))
        if idx is not None:
            hits.setdefault(idx, []).append(str(pid))

    def enrich(i: int, item: dict) -> dict:
        found = hits.get(item_ids[i], []) if item_ids[i] is not None else []
        pid = found[0] if len(found) == 1 else None
        if pid is None:
            status = "unmatched"
        elif pid in mine:
            status = "yours"
        elif pid in rostered:
            status = "rostered"
        else:
            status = "available"
        lo, hi = item.get("faab_pct_low"), item.get("faab_pct_high")
        return {
            **item,
            "player_id": pid,
            "status": status,
            "faab_low": round(lo / 100 * budget_total) if lo is not None else None,
            "faab_high": round(hi / 100 * budget_total) if hi is not None else None,
        }

    n = len(report["priority"])
    return {
        "available": True,
        "title": report["title"],
        "week": report["week"],
        "url": report["url"],
        "fetched_at": report["fetched_at"],
        "budget_total": budget_total,
        "priority": [enrich(i, it) for i, it in enumerate(report["priority"])],
        "targets": [enrich(n + i, it) for i, it in enumerate(report["targets"])],
        "defenses": report["defenses"],
        "unparsed": report["unparsed"],
    }


def _free_agent_board(
    client, week: int | None, budget_total: int, remaining: int | None, claims: list
) -> dict[str, Any]:
    """Top projected free agents per position, each with a market price and a
    value ceiling - the two numbers `fcc faab bid` shows for one named player,
    precomputed here for everyone actually available to add right now.
    """
    from fcc.engines.faab import BidContext, recommend_bid
    from fcc.engines.lineup import NON_STARTING, eligible_positions
    from fcc.engines.market import BidMarket
    from fcc.engines.value import (
        RankedPlayer,
        rank_lookup,
        top_free_agents_by_position,
        value_over_replacement,
    )
    from fcc.sources.fantasyguru import load_projections, load_rankings

    # Read from the cache `fcc fantasyguru refresh` writes; a dashboard request
    # never launches a browser against the subscriber site.
    expert = load_rankings()
    expert_rank = rank_lookup(expert["positions"]) if expert else (lambda _p: None)
    fg_projections = load_projections()

    try:
        week = week or client.current_week() or 1
        ranked = client.ranked_players(week)
        rostered = client.rostered_player_ids()
        board = top_free_agents_by_position(ranked, rostered, limit=10)

        # Sleeper projects every NFL position, including IDP slots (DL/LB/DB/...)
        # this league may not even roster. Only show positions a real slot in
        # this league's roster_positions can actually use.
        relevant: set[str] = set()
        for slot in client.league().get("roster_positions") or []:
            if slot in NON_STARTING:
                continue
            relevant |= eligible_positions(slot) or set()
        if relevant:
            board = {pos: rows for pos, rows in board.items() if pos in relevant}

        total_rosters = client.league().get("total_rosters") or 0
        weeks_left = max(1, 18 - week)
        remaining = remaining if remaining is not None else budget_total

        def ceiling(vor: float | None) -> dict | None:
            if vor is None:
                return None
            bid = recommend_bid(vor, BidContext(budget_total, remaining, weeks_left), None)
            return {"low": bid.low, "target": bid.target, "high": bid.high} if bid else None

        # FantasyGuru's projections as a second, independent pool: its own
        # replacement level, its own VOR. Matched to Sleeper players by
        # position + name + team; an unmatched player shows no FG numbers
        # rather than borrowing someone else's.
        rec = (client.league().get("scoring_settings") or {}).get("rec") or 0
        scoring = "ppr" if rec >= 1 else "half_ppr" if rec >= 0.5 else "std"
        fg_pool = [
            RankedPlayer(name=r["name"], position=r["position"], team=r["team"],
                         projected_points=r[scoring])
            for r in (fg_projections or {}).get("players", [])
        ]
        fg_index = rank_lookup({
            pos: [{"name": p.name, "team": p.team, "rank": i}
                  for i, p in enumerate(fg_pool) if p.pos == pos]
            for pos in {p.pos for p in fg_pool}
        })

        def fg_lookup(player: RankedPlayer) -> RankedPlayer | None:
            i = fg_index(player)
            return fg_pool[i] if i is not None else None

        by_position: dict[str, list[dict]] = {}
        for pos, group in board.items():
            pos_market = BidMarket(claims=claims).comparable(pos)
            rows = []
            for p in group:
                vor = value_over_replacement(p, ranked, total_rosters) if total_rosters else None
                fg = fg_lookup(p)
                fg_vor = (
                    value_over_replacement(fg, fg_pool, total_rosters)
                    if fg is not None and total_rosters else None
                )
                rows.append(
                    {
                        "player_id": p.player_id,
                        "name": p.name,
                        "team": p.team,
                        "projected_points": p.projected_points,
                        "vor": vor,
                        "market_price": pos_market.price_for_win_probability(0.65, budget_total),
                        "recommend": ceiling(vor),
                        "expert_rank": expert_rank(p),
                        "fg_projected_points": fg.projected_points if fg else None,
                        "fg_vor": fg_vor,
                        "fg_recommend": ceiling(fg_vor),
                    }
                )
            by_position[pos] = rows
        return {
            "week": week,
            "by_position": by_position,
            "expert": {"fetched_at": expert["fetched_at"]} if expert else None,
            "fg_projections": (
                {"fetched_at": fg_projections["fetched_at"]} if fg_projections else None
            ),
        }
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        log.warning("free-agent board unavailable: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/actions")
def actions(status: str | None = None, limit: int = 100) -> list[dict]:
    with session_scope() as session:
        stmt = select(Action).order_by(Action.created_at.desc()).limit(limit)
        if status:
            try:
                stmt = stmt.where(Action.status == ActionStatus(status))
            except ValueError as exc:
                raise HTTPException(400, f"Unknown status {status!r}") from exc
        return [_action_json(a) for a in session.scalars(stmt)]


@app.get("/api/actions/pending")
def pending() -> list[dict]:
    """Money-tier actions waiting on you. This is the dashboard's whole job."""
    with session_scope() as session:
        return [_action_json(a) for a in ActionGate.pending_approval(session)]


@app.post("/api/actions/{action_id}/approve")
def approve(action_id: int) -> dict:
    with session_scope() as session:
        action = session.get(Action, action_id)
        if action is None:
            raise HTTPException(404, f"No action {action_id}")
        if action.is_terminal:
            raise HTTPException(409, f"Action {action_id} is already {action.status.value}")
        ActionGate().approve(session, action)
        return _action_json(action)


@app.post("/api/actions/{action_id}/reject")
def reject(action_id: int, decision: Decision) -> dict:
    with session_scope() as session:
        action = session.get(Action, action_id)
        if action is None:
            raise HTTPException(404, f"No action {action_id}")
        if action.is_terminal:
            raise HTTPException(409, f"Action {action_id} is already {action.status.value}")
        ActionGate().reject(session, action, decision.reason)
        return _action_json(action)


@app.get("/api/overview")
def overview() -> dict:
    """The 'what needs my attention' feed the landing tab renders."""
    league_rows = leagues()
    with session_scope() as session:
        waiting = [_action_json(a) for a in ActionGate.pending_approval(session)]
        recent_failures = [
            _action_json(a)
            for a in session.scalars(
                select(Action)
                .where(Action.status.in_([ActionStatus.FAILED, ActionStatus.UNVERIFIED]))
                .order_by(Action.created_at.desc())
                .limit(20)
            )
        ]
    return {
        "dry_run": get_settings().dry_run,
        "needs_approval": waiting,
        "needs_attention": recent_failures,
        "leagues": league_rows,
        "league_errors": [r for r in league_rows if r.get("error") and r.get("supported")],
    }


# --------------------------------------------------------------------------
# static frontend
# --------------------------------------------------------------------------
def mount_frontend(application: FastAPI) -> bool:
    """Serve the built dashboard, if it has been built.

    Mounted last so it can claim "/" without shadowing any /api route. Absent
    build output is not an error — the API is useful on its own, and `npm run
    dev` proxies to it during frontend work.
    """
    dist = get_settings().repo_root / "web" / "dist"
    if not (dist / "index.html").exists():
        return False

    application.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @application.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(dist / "index.html")

    return True


@app.exception_handler(Exception)
def _unhandled(_request, exc: Exception) -> JSONResponse:
    log.exception("unhandled API error")
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


FRONTEND_MOUNTED = mount_frontend(app)
