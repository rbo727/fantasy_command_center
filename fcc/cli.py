"""Command line interface.

`fcc doctor` is the important one: run it after every deploy and before trusting
any unattended job. It answers "will the 3am run actually work" while you're
awake to do something about the answer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from fcc.core.config import get_settings
from fcc.core.secrets import Keys, SecretsError, SecretStore

app = typer.Typer(add_completion=False, help="Fantasy Command Center")
secrets_app = typer.Typer(help="Manage encrypted credentials")
pickem_app = typer.Typer(help="ESPN pick'em automation")
lineup_app = typer.Typer(help="Lineup guardian")
faab_app = typer.Typer(help="FAAB bidding")
app.add_typer(secrets_app, name="secrets")
app.add_typer(pickem_app, name="pickem")
app.add_typer(lineup_app, name="lineup")
app.add_typer(faab_app, name="faab")

console = Console()


@app.callback()
def _configure(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else get_settings().log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
@app.command()
def doctor() -> None:
    """Check that this host can actually do the work."""
    settings = get_settings()
    table = Table(title="fcc doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")

    failures = 0

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        nonlocal failures
        if ok is None:
            table.add_row(name, "[yellow]skip[/yellow]", detail)
        elif ok:
            table.add_row(name, "[green]ok[/green]", detail)
        else:
            failures += 1
            table.add_row(name, "[red]FAIL[/red]", detail)

    row("data dir", settings.data_path.exists(), str(settings.data_path))
    row("timezone", True, settings.timezone)
    row(
        "dry run",
        True,
        "[yellow]ON — no writes will leave this process[/yellow]"
        if settings.dry_run
        else "[red]OFF — writes are live[/red]",
    )

    # Database
    try:
        from fcc.core.db import init_db, session_scope
        from fcc.core.models import Action

        init_db()
        with session_scope() as s:
            count = s.query(Action).count()
        row("database", True, f"{settings.db_url} ({count} actions recorded)")
    except Exception as exc:  # noqa: BLE001
        row("database", False, str(exc))

    # Secrets
    try:
        store = SecretStore()
        names = store.names()
        row("secret store", True, f"{len(names)} secrets: {', '.join(names) or '(empty)'}")
        for label, key in [
            ("ESPN swid", Keys.ESPN_SWID),
            ("ESPN s2", Keys.ESPN_S2),
            ("FantasyGuru login", Keys.FANTASYGURU_USERNAME),
            ("Sleeper token", Keys.SLEEPER_TOKEN),
        ]:
            row(label, store.has(key) or None, "set" if store.has(key) else "not configured")
    except SecretsError as exc:
        row("secret store", False, str(exc))

    # Leagues
    leagues = settings.leagues()
    row(
        "leagues config",
        bool(leagues) or None,
        f"{len(leagues)} configured" if leagues else f"none in {settings.leagues_file}",
    )

    # ESPN pick'em reachability
    try:
        store = SecretStore()
        if store.has(Keys.ESPN_SWID) and store.has(Keys.ESPN_S2):
            from fcc.platforms.espn_pickem import ESPNPickemClient

            pickem_leagues = [lg for lg in leagues if lg.platform == "espn_pickem"]
            if not pickem_leagues:
                row("ESPN pick'em", None, "no espn_pickem league configured")
            for lg in pickem_leagues:
                with ESPNPickemClient(
                    store.require(Keys.ESPN_SWID),
                    store.require(Keys.ESPN_S2),
                    lg.extra.get("challenge", lg.league_id),
                ) as client:
                    props = client.propositions()
                    unresolved = [p.label for p in props if not p.fully_resolved]
                    row(
                        f"ESPN pick'em [{lg.key}]",
                        not unresolved,
                        f"{len(props)} propositions"
                        + (f"; UNRESOLVED: {unresolved}" if unresolved else ""),
                    )
        else:
            row("ESPN pick'em", None, "ESPN cookies not configured")
    except Exception as exc:  # noqa: BLE001
        row("ESPN pick'em", False, f"{type(exc).__name__}: {exc}")

    # Write spec
    spec_path = settings.data_path / "espn_pickem_write.json"
    row(
        "pick'em write path",
        spec_path.exists() or None,
        str(spec_path) if spec_path.exists() else "not captured — see `fcc pickem capture-write`",
    )

    console.print(table)
    if failures:
        console.print(f"[red]{failures} check(s) failed.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
@secrets_app.command("init")
def secrets_init() -> None:
    """Generate a master key. Store it in your password manager."""
    key = SecretStore.generate_key()
    console.print("Add this to your environment (or .env) on this host:\n")
    console.print(f"[bold]FCC_MASTER_KEY={key}[/bold]\n")
    console.print(
        "[yellow]This key is never stored by fcc. Lose it and the secret store "
        "must be rebuilt from scratch.[/yellow]"
    )


@secrets_app.command("set")
def secrets_set(
    name: str,
    value: str = typer.Option(None, help="Omit to be prompted without echo."),
) -> None:
    """Store a credential."""
    if value is None:
        value = typer.prompt(f"Value for {name}", hide_input=True)
    SecretStore().set(name, value)
    console.print(f"[green]Stored {name}.[/green]")


@secrets_app.command("list")
def secrets_list() -> None:
    """List stored secret names (never values)."""
    for name in SecretStore().names():
        console.print(f"  {name}")


@secrets_app.command("rm")
def secrets_rm(name: str) -> None:
    SecretStore().delete(name)
    console.print(f"[green]Removed {name}.[/green]")


# --------------------------------------------------------------------------
# pick'em
# --------------------------------------------------------------------------
def _pickem_client(league_key: str):
    from fcc.platforms.espn_pickem import ESPNPickemClient, WriteSpec

    settings = get_settings()
    league = settings.league(league_key)
    store = SecretStore()
    spec = WriteSpec.load(settings.data_path / "espn_pickem_write.json")
    return ESPNPickemClient(
        store.require(Keys.ESPN_SWID),
        store.require(Keys.ESPN_S2),
        league.extra.get("challenge", league.league_id),
        write_spec=spec,
    ), league


@pickem_app.command("dump")
def pickem_dump(
    league: str = typer.Argument(..., help="League key from config/leagues.yml"),
    week: int = typer.Option(None),
    out: Path = typer.Option(None, help="Where to write the JSON"),
    redact_output: bool = typer.Option(
        True,
        "--redact/--raw",
        help="Strip personal identifiers, keeping structure and NFL content. "
        "Use --raw only for a file that stays on this machine.",
    ),
) -> None:
    """Save ESPN responses as a fixture.

    Run this once against your real account so the parsers can be validated
    against ESPN's actual field names rather than assumed ones. Redacted by
    default, so the result is safe to share.
    """
    from fcc.platforms.redact import redact_document

    client, lg = _pickem_client(league)
    with client:
        payload = {
            "challenge": client.challenge,
            "week": week,
            "challenge_info": client.challenge_info(week=week),
            "entry": client.entry(),
        }

    suffix = "" if redact_output else "_raw"
    target = out or (
        get_settings().data_path / f"pickem_dump_{lg.key}_w{week or 'cur'}{suffix}.json"
    )

    if redact_output:
        payload, report = redact_document(payload)
        target.write_text(json.dumps(payload, indent=2))

        table = Table(title="Redacted", show_lines=False)
        table.add_column("Path")
        table.add_column("Values", justify="right")
        for path, count in sorted(report.paths.items()):
            table.add_row(path, str(count))
        console.print(table)
        console.print(f"[green]Wrote {target}[/green]")
        console.print(
            f"[dim]{report.total} identifier(s) replaced with stable pseudonyms. "
            f"Kept {len(set(report.kept_names))} NFL name(s) such as "
            f"{', '.join(sorted(set(report.kept_names))[:3]) or '(none found)'}.[/dim]"
        )
        console.print(
            "[cyan]Structure, key names and types are unchanged, so this is still a "
            "valid fixture — and it is safe to share.[/cyan]"
        )
        console.print(
            "[yellow]Skim it before sending anywhere: redaction is default-deny, but "
            "only you can recognise something of yours that ESPN put in an "
            "unexpected field.[/yellow]"
        )
    else:
        target.write_text(json.dumps(payload, indent=2))
        console.print(f"[green]Wrote {target}[/green]")
        console.print(
            "[red]UNREDACTED — contains your ESPN account identifiers and other "
            "entrants' names. Keep this file local.[/red]"
        )


@pickem_app.command("capture-write")
def pickem_capture_write(
    curl_file: Path = typer.Option(..., help="File containing a 'Copy as cURL' command"),
) -> None:
    """Teach fcc how to submit picks, from a request you captured."""
    from fcc.platforms.curl_import import CurlParseError, build_write_spec
    from fcc.platforms.espn_pickem import WriteSpec

    try:
        spec_kwargs, report = build_write_spec(curl_file.read_text())
    except CurlParseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    path = get_settings().data_path / "espn_pickem_write.json"
    WriteSpec(**spec_kwargs).save(path)

    table = Table(title="Captured write request")
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    for k, v in report.items():
        table.add_row(k, str(v))
    console.print(table)
    if report["dropped_headers"]:
        console.print(
            f"[yellow]Dropped credential headers ({', '.join(report['dropped_headers'])}) — "
            "fcc sends your stored ESPN cookies instead.[/yellow]"
        )
    console.print(f"[green]Saved to {path}[/green]")


@pickem_app.command("run")
def pickem_run(
    league: str = typer.Argument(..., help="League key from config/leagues.yml"),
    week: int = typer.Option(None, help="Defaults to the current scoring period"),
    picks_file: Path = typer.Option(
        None,
        help="JSON list of staff picks, bypassing FantasyGuru: "
        "[{matchup, team, market, spread, conviction}]",
    ),
    fg_url: str = typer.Option(
        None,
        help="FantasyGuru staff-picks page. Defaults to the league's extra.fantasyguru_url.",
    ),
    refresh: bool = typer.Option(False, help="Ignore the cached FantasyGuru page"),
    confidence: bool = typer.Option(False, help="Assign confidence points 1..N"),
    submit: bool = typer.Option(False, help="Actually submit (otherwise preview only)"),
) -> None:
    """Match staff picks to this week's slate and submit them."""
    from fcc.core.actions import ActionGate, Proposal, Verification
    from fcc.core.db import init_db, session_scope
    from fcc.core.models import ActionKind
    from fcc.engines.pickem import StaffPick, join

    settings = get_settings()
    init_db()
    client, lg = _pickem_client(league)

    if picks_file:
        staff = [StaffPick(**item) for item in json.loads(picks_file.read_text())]
    else:
        from fcc.sources.fantasyguru import (
            FantasyGuruError,
            FantasyGuruSession,
            extract_staff_picks,
        )

        url = fg_url or lg.extra.get("fantasyguru_url")
        if not url:
            console.print(
                "[red]No source for picks. Pass --picks-file, --fg-url, or set "
                "extra.fantasyguru_url on this league in config/leagues.yml.[/red]"
            )
            raise typer.Exit(code=1)
        try:
            html = FantasyGuruSession().fetch(url, force=refresh)
            staff, notes = extract_staff_picks(html, week=week)
        except FantasyGuruError as exc:
            console.print(f"[red]FantasyGuru: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[dim]FantasyGuru: {len(staff)} picks extracted from {url}[/dim]")
        if notes:
            console.print(f"[yellow]Extraction notes: {notes}[/yellow]")
        if not staff:
            console.print(
                "[red]No picks found on that page. Check the URL, or whether the "
                "login/paywall is blocking it.[/red]"
            )
            raise typer.Exit(code=1)

    with client:
        props = client.propositions(week=week)
        result = join(staff, props, use_confidence_points=confidence)

        table = Table(title=f"{lg.key} week {week or 'current'} — {result.summary()}")
        table.add_column("Game")
        table.add_column("Pick")
        table.add_column("P(win)")
        table.add_column("Conf")
        for p in result.picks:
            table.add_row(
                next((q.label for q in props if q.id == p.proposition_id), p.proposition_id),
                p.team_code,
                f"{result.confidence_by_prop.get(p.proposition_id, 0):.3f}",
                str(p.confidence or "-"),
            )
        console.print(table)

        if result.review:
            console.print("\n[yellow]Needs review — these were NOT submitted:[/yellow]")
            for item in result.review:
                console.print(f"  [{item.reason}] {item.detail}")

        if not submit:
            console.print("\n[cyan]Preview only. Re-run with --submit to send.[/cyan]")
            return
        if not result.picks:
            console.print("[yellow]Nothing to submit.[/yellow]")
            return

        gate = ActionGate()
        payload = {
            "picks": [
                {
                    "proposition": p.proposition_id,
                    "option": p.option_id,
                    "team": p.team_code,
                    "confidence": p.confidence,
                }
                for p in result.picks
            ]
        }
        # Sorted so the same intent always yields the same key, whatever order
        # the engine happened to emit picks in.
        ordered = sorted(result.picks, key=lambda x: x.proposition_id)
        key = f"pickem:{lg.key}:{week or 'cur'}:" + ",".join(
            f"{p.proposition_id}={p.option_id}" for p in ordered
        )

        def _submit(_action) -> dict:
            return client.submit_picks(result.picks)

        def _verify(_action) -> Verification:
            recorded = client.existing_picks()
            if not recorded:
                # ESPN withholds selections until kickoff for some challenges,
                # so an empty read-back is inconclusive, not proof of failure.
                return Verification(
                    ok=False,
                    message=(
                        "ESPN returned no readable picks; selections may be "
                        "hidden until kickoff"
                    ),
                    detail={"read_back": {}},
                )
            mismatches = {
                p.proposition_id: recorded.get(p.proposition_id)
                for p in result.picks
                if recorded.get(p.proposition_id) != p.option_id
            }
            return Verification(
                ok=not mismatches,
                message=f"{len(mismatches)} pick(s) did not match" if mismatches else "",
                detail={"mismatches": mismatches, "read_back_count": len(recorded)},
            )

        with session_scope() as session:
            action = gate.propose_and_execute(
                session,
                Proposal(
                    kind=ActionKind.PICKEM_SUBMIT,
                    idempotency_key=key,
                    payload=payload,
                    rationale=(
                        f"{len(result.picks)} staff-derived picks; "
                        f"{len(result.review)} to review"
                    ),
                    league_key=lg.key,
                    season=lg.season,
                    week=week,
                ),
                submit=_submit,
                verify=_verify,
            )
            console.print(f"\nAction {action.id}: [bold]{action.status.value}[/bold]")
            if action.error:
                console.print(f"[red]{action.error}[/red]")
            if settings.dry_run:
                console.print("[yellow]FCC_DRY_RUN is on — nothing was sent.[/yellow]")


# --------------------------------------------------------------------------
# FAAB
# --------------------------------------------------------------------------
def _sleeper_for(league_key: str):
    from fcc.platforms.registry import connector_for

    settings = get_settings()
    lg = settings.league(league_key)
    if lg.platform != "sleeper":
        raise typer.BadParameter(
            f"{league_key} is a {lg.platform} league; FAAB history is only wired "
            "up for Sleeper so far."
        )
    return connector_for(lg), lg


def _find_player(client, name: str) -> dict:
    """Resolve a player name to one index entry, or refuse.

    Refuses on an ambiguous name rather than picking the first match - bidding
    on the wrong player is an expensive way to find out.
    """
    needle = name.strip().lower()
    index = client.players()
    matches = [
        (pid, raw) for pid, raw in index.items()
        if (raw.get("full_name") or "").lower() == needle
    ]
    if not matches:
        matches = [
            (pid, raw) for pid, raw in index.items()
            if needle in (raw.get("full_name") or "").lower()
        ]
    rostered = [(pid, raw) for pid, raw in matches if raw.get("team")]
    if rostered:
        matches = rostered

    if not matches:
        raise typer.BadParameter(f"No player matching {name!r} in the Sleeper index.")
    if len(matches) > 1:
        listing = ", ".join(
            f"{r.get('full_name')} ({r.get('position')}, {r.get('team')})"
            for _, r in matches[:8]
        )
        raise typer.BadParameter(f"{name!r} is ambiguous: {listing}. Be more specific.")
    pid, raw = matches[0]
    return {"id": pid, **raw}


def _money(value) -> str:
    return f"${value:.0f}" if value is not None else "-"


def _render_market(market) -> None:
    s = market.summary()
    table = Table(title=f"League bid market - {s['segment']}")
    table.add_column("Measure")
    table.add_column("Value", justify="right")
    table.add_row("Claims seen", str(s["claims"]))
    table.add_row("Won by someone", str(s["resolved"]))
    table.add_row("Median winning bid", _money(s["median_winning_bid"]))
    table.add_row("Biggest winning bid", _money(s["max_winning_bid"]))
    table.add_row("Median runner-up", _money(s["median_runner_up"]))
    table.add_row(
        "Claims with >1 bidder",
        f"{s['contest_rate']:.0%}" if s["contest_rate"] is not None else "-",
    )
    table.add_row(
        "Winner's premium over field",
        f"{s['median_overpay']:.0%}" if s["median_overpay"] is not None else "-",
    )
    console.print(table)
    if not s["usable"]:
        console.print(
            "[yellow]Thin history - treat the ladder below as indicative only.[/yellow]"
        )


@faab_app.command("market")
def faab_market(
    league: str = typer.Argument(..., help="League key from config/leagues.yml"),
    position: str = typer.Option(None, help="Narrow to one position, e.g. TE"),
) -> None:
    """Show what winning a waiver has actually cost in this league."""
    from fcc.engines.market import BidMarket, parse_claims

    client, _lg = _sleeper_for(league)
    with client:
        claims = parse_claims(client.transaction_history(), client.players())
        market = BidMarket(claims=claims)
        if position:
            market = market.comparable(position)
        _render_market(market)

        contested = sorted(
            (c for c in market.claims if c.contested and c.winning_bid is not None),
            key=lambda c: c.winning_bid,
            reverse=True,
        )[:10]
        if contested:
            t = Table(title="Most expensive contested claims")
            for col in ("Week", "Player", "Pos"):
                t.add_column(col)
            t.add_column("Won", justify="right")
            t.add_column("Runner-up", justify="right")
            for c in contested:
                t.add_row(
                    str(c.week or "?"), c.player_name, c.position or "",
                    f"${c.winning_bid}", f"${c.runner_up}",
                )
            console.print(t)


@faab_app.command("bid")
def faab_bid(
    league: str = typer.Argument(..., help="League key from config/leagues.yml"),
    player: str = typer.Option(..., "--player", help="Player you want to add"),
    need: str = typer.Option(
        "depth",
        help="replacing_injured_starter | starter_upgrade | depth | speculative",
    ),
    vor: float = typer.Option(
        None,
        help="Projected points per week above a freely-available replacement. "
        "Optional; without it you get the market answer only.",
    ),
    weeks_remaining: int = typer.Option(None, help="Defaults to 18 minus the current week"),
) -> None:
    """What would it take to win this player, based on your league's own bids."""
    from fcc.engines.faab import BidContext, recommend_bid
    from fcc.engines.market import BidMarket, parse_claims

    client, _lg = _sleeper_for(league)
    with client:
        target = _find_player(client, player)
        budget_total = (client.league().get("settings") or {}).get("waiver_budget") or 0
        summary = client.league_summary()
        remaining = (
            summary.faab_remaining if summary.faab_remaining is not None else budget_total
        )
        week = client.current_week() or 1
        weeks_left = weeks_remaining if weeks_remaining is not None else max(1, 18 - week)

        console.print(
            "\n[bold]{}[/bold] ({}, {}) - week {}, ${} of ${} FAAB left, "
            "{} weeks remaining\n".format(
                target.get("full_name"), target.get("position"),
                target.get("team") or "FA", week, remaining, budget_total, weeks_left,
            )
        )

        claims = parse_claims(client.transaction_history(), client.players())
        market = BidMarket(claims=claims).comparable(target.get("position"))
        _render_market(market)

        ladder = Table(title="What this league's history says it takes")
        ladder.add_column("If you bid", justify="right")
        ladder.add_column("Would have won", justify="right")
        ladder.add_column("Of budget left", justify="right")
        shown = False
        for prob in (0.5, 0.65, 0.8, 0.9, 1.0):
            price = market.price_for_win_probability(prob, budget_total)
            if price is None:
                continue
            shown = True
            over = "  [red](over budget)[/red]" if price > remaining else ""
            ladder.add_row(
                f"${price}{over}",
                f"{prob:.0%} of comparable claims",
                f"{price / remaining:.0%}" if remaining else "-",
            )
        if shown:
            console.print(ladder)
        else:
            console.print("[yellow]No resolved claims yet - no market to read.[/yellow]")

        if vor is not None:
            rec = recommend_bid(
                vor, BidContext(budget_total, remaining, weeks_left, need=need), None
            )
            if rec:
                console.print(
                    f"\n[bold]Value ceiling:[/bold] {rec.describe()} - what he is worth "
                    "to this roster, independent of what winning costs."
                )
                console.print(f"[dim]{rec.rationale}[/dim]")
                console.print(
                    "\n[cyan]Bid where the two agree. If the market price exceeds the "
                    "value ceiling, he is going for more than he is worth to you - that "
                    "is a pass, not a stretch.[/cyan]"
                )
        else:
            console.print(
                "\n[dim]Pass --vor to also get the value ceiling (what he is worth to "
                "you, as opposed to what winning costs).[/dim]"
            )

        console.print(
            "\n[yellow]FAAB is money tier: nothing here is submitted. "
            "This is advice for you to act on.[/yellow]"
        )


# --------------------------------------------------------------------------
# lineup guardian
# --------------------------------------------------------------------------
@lineup_app.command("check")
def lineup_check(
    league: str = typer.Argument(None, help="League key; omit to check every league"),
    week: int = typer.Option(None),
    submit: bool = typer.Option(
        False, help="Submit the swaps (requires a platform write connector)"
    ),
) -> None:
    """Find starters who cannot play, and who should replace them."""
    from fcc.core.actions import ActionGate, Proposal
    from fcc.core.db import init_db, session_scope
    from fcc.core.models import ActionKind
    from fcc.engines.lineup import plan_lineup
    from fcc.platforms.registry import UnsupportedPlatform, connector_for

    settings = get_settings()
    init_db()
    keys = [league] if league else [lg.key for lg in settings.leagues() if lg.enabled]
    if not keys:
        console.print("[yellow]No leagues configured.[/yellow]")
        raise typer.Exit(code=1)

    any_problem = False
    for key in keys:
        try:
            lg = settings.league(key)
            connector = connector_for(lg)
            roster = connector.roster(week=week)
        except UnsupportedPlatform as exc:
            console.print(f"[dim]{key}: {exc}[/dim]")
            continue
        except Exception as exc:  # noqa: BLE001 - one league must not stop the sweep
            console.print(f"[red]{key}: {type(exc).__name__}: {exc}[/red]")
            continue

        plan = plan_lineup(roster, week=week)
        console.print(f"\n[bold]{key}[/bold] — week {roster.week or '?'} — {plan.summary()}")

        if plan.swaps:
            any_problem = True
            table = Table(show_lines=False)
            table.add_column("Slot")
            table.add_column("Out")
            table.add_column("Why")
            table.add_column("In")
            for swap in plan.swaps:
                table.add_row(
                    swap.slot,
                    swap.out_player.name,
                    swap.out_player.status.value,
                    swap.in_player.name,
                )
            console.print(table)

        for warning in plan.warnings:
            any_problem = True
            console.print(f"  [yellow]{warning.kind}[/yellow] {warning.detail}")

        if plan.clean:
            console.print("  [green]Lineup is clean.[/green]")
            continue

        # Record the intent even when we cannot act on it, so the dashboard and
        # the audit trail show the guardian did look.
        with session_scope() as session:
            gate = ActionGate()
            for swap in plan.swaps:
                gate.propose(
                    session,
                    Proposal(
                        kind=ActionKind.LINEUP_SWAP,
                        idempotency_key=(
                            f"lineup:{key}:{roster.week}:{swap.slot_index}:"
                            f"{swap.out_player.player_id}->{swap.in_player.player_id}"
                        ),
                        payload={
                            "slot": swap.slot,
                            "out": swap.out_player.name,
                            "out_id": swap.out_player.player_id,
                            "in": swap.in_player.name,
                            "in_id": swap.in_player.player_id,
                        },
                        rationale=swap.reason,
                        league_key=key,
                        season=lg.season,
                        week=roster.week,
                    ),
                )

        if submit:
            console.print(
                "[yellow]No write connector exists for this platform yet — lineup "
                "submission lands with the Sleeper/Yahoo write path in Stage 4. The "
                "swaps above are recorded and were not sent.[/yellow]"
            )

    if any_problem:
        raise typer.Exit(code=1)   # non-zero so a cron wrapper can alert on it


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address. Localhost by default — this server holds write access "
        "to your leagues, so reach it over an SSH tunnel rather than exposing it.",
    ),
    port: int = typer.Option(8765),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)"),
) -> None:
    """Run the dashboard."""
    import uvicorn

    from fcc.api.app import FRONTEND_MOUNTED

    if not FRONTEND_MOUNTED:
        console.print(
            "[yellow]The dashboard front end has not been built — serving the API only.\n"
            "Build it with `cd web && npm install && npm run build`, or run "
            "`npm run dev` in another terminal for hot reload.[/yellow]"
        )
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[red]Binding to {host}, not localhost. This server can change your "
            "lineups and spend FAAB — make sure something in front of it is doing "
            "authentication.[/red]"
        )
    console.print(f"[green]http://{host}:{port}[/green]")
    uvicorn.run("fcc.api.app:app", host=host, port=port, reload=reload)


@app.command("db-init")
def db_init() -> None:
    """Create database tables."""
    from fcc.core.db import init_db

    init_db()
    console.print(f"[green]Initialised {get_settings().db_url}[/green]")


if __name__ == "__main__":
    app()
