# Fantasy Command Center (`ffm`)

One self-hosted service across Yahoo, Sleeper, ESPN fantasy, ESPN pick'em and a
survivor pool: pulls recommendations, submits the low-risk decisions
unattended, and proposes the ones that cost money.

```bash
git clone https://github.com/rbo727/fantasy_command_center
```

> **Previously named `yahoo_fantasy_api`.** This repository began as a fork of
> [spilchen/yahoo_fantasy_api](https://github.com/spilchen/yahoo_fantasy_api)
> and was renamed once it became its own project. GitHub redirects the old URL
> permanently, so existing clones and remotes keep working — though
> `git remote set-url origin https://github.com/rbo727/fantasy_command_center`
> is worth running to avoid confusion.
>
> That library still lives in `yahoo_fantasy_api/` and tracks upstream
> unmodified — it's a dependency, not the project. The application is `ffm/`.

## Repository layout

Two projects share this repository: the application, and the vendored Yahoo
library it depends on. Knowing which is which saves a lot of confusion.

| Path | What it is |
|---|---|
| `ffm/` | **The application.** Everything Fantasy Command Center does. |
| `tests/` | Application tests. |
| `docs/SETUP.md` | **How to run it — start here.** |
| `config/` | Your league declarations (`leagues.example.yml`). |
| `yahoo_fantasy_api/` | Vendored upstream library. Tracks spilchen unmodified — do not rename this directory, `import yahoo_fantasy_api` depends on it. |
| `README.rst`, `docs/*.rst`, `docs/conf.py`, `setup.py`, `.readthedocs.yaml`, `requirements.txt` | The **vendored library's** own docs and packaging, describing the Yahoo API bindings rather than this project. Left untouched so `git merge upstream/master` stays conflict-free. |

The application is packaged by `pyproject.toml`; `setup.py` belongs to the
vendored library and is not this project's build config.

## Status

| Stage | Scope | State |
|---|---|---|
| 0 | Foundations: config, secrets, DB, action gate, team resolution | done |
| 1 | **ESPN pick'em from FantasyGuru staff picks** | complete pending two live steps: capture the ESPN write request, and a first run against the real FantasyGuru page |
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
