# Fantasy Command Center (`ffm`)

One self-hosted service across Yahoo, Sleeper, ESPN fantasy, ESPN pick'em and a
survivor pool: pulls recommendations, submits the low-risk decisions
unattended, and proposes the ones that cost money.

> This repository began as a fork of
> [spilchen/yahoo_fantasy_api](https://github.com/spilchen/yahoo_fantasy_api).
> That library still lives in `yahoo_fantasy_api/` and tracks upstream
> unmodified — it's a dependency, not the project. The application is `ffm/`.

## Status

| Stage | Scope | State |
|---|---|---|
| 0 | Foundations: config, secrets, DB, action gate, team resolution | done |
| 1 | **ESPN pick'em from FantasyGuru staff picks** | client + engine + CLI done; FantasyGuru source next |
| 2 | Dashboard (FastAPI + React tabs) | not started |
| 3 | Lineup guardian (never start an inactive player) | not started |
| 4 | FAAB engine + 3am Wednesday Sleeper waiver sniper | not started |
| 5 | Survivor engine (win prob × popularity × future value) | not started |
| 6 | Harden and deploy to the seedbox | not started |

See `docs/SETUP.md` to get running.

## Design rules

These are the things that keep an unattended system from quietly costing you a
week:

**Everything goes through the action gate.** No engine calls a platform's write
API directly. `ffm/core/actions.py` owns the state machine, so every platform
behaves identically and every mutation is auditable in the `actions` table.

**A write that isn't confirmed is not a success.** After submitting, ffm reads
the platform back. If the read-back disagrees — or can't be performed — the
action is recorded `unverified` and you get notified. Silent wrongness is the
real enemy; a crashed job is obvious, a job that submitted the *wrong* picks is
not.

**Resolve or refuse.** `ffm/core/teams.py` maps team labels across nflverse,
ESPN (`WSH`, `JAC`), Yahoo and Sleeper spellings. It has no fuzzy matching:
ambiguous input like "New York" returns `None` and routes to human review.

**Markets aren't interchangeable.** FantasyGuru's staff picks are frequently
against the spread; ESPN pick'em asks who wins outright. An ATS lean on a +7
underdog is usually a straight-up *loss* pick, so `ffm/engines/pickem.py`
converts spread → win probability and picks accordingly, rather than mirroring
the staff card.

**Money needs a human.** Pick'em submits and benching an OUT player run
unattended. FAAB bids, survivor picks, drops and trades always require
approval, and there is deliberately no setting that changes that.

**Dry run is the default.** A fresh checkout or a freshly provisioned seedbox
cannot touch a live league until you explicitly set `FFM_DRY_RUN=false`.

## Development

```bash
pip install -e ".[dev]"
pytest          # app tests
ruff check ffm tests
pytest yahoo_fantasy_api/tests   # vendored library, tracks upstream
```

Keeping the vendored library current:

```bash
git fetch upstream && git merge upstream/master
```
