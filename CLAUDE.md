# Fantasy Command Center — working notes

Cross-platform fantasy football manager: Yahoo, Sleeper, ESPN fantasy, ESPN
pick'em, survivor. Pulls recommendations, submits low-risk decisions unattended,
proposes the ones that cost money. Runs on a home PC in development and a Linux
seedbox in production.

## Layout

| Path | What it is |
|---|---|
| `fcc/` | The application. |
| `yahoo_fantasy_api/` | **Vendored upstream library — do not edit.** Tracks `spilchen/yahoo_fantasy_api` via `git merge upstream/master`. Editing it puts a conflict in every future merge. Its `docs/*.rst`, `README.rst`, `setup.py` and `.readthedocs.yaml` belong to it too. |
| `web/` | Vite + React dashboard. |
| `config/leagues.yml` | Your leagues (gitignored; `.example.yml` is the template). |
| `data/` | Runtime state — DB, caches, `secrets.enc`. Gitignored. |

`pyproject.toml` packages the app. `setup.py` is the vendored library's and is
not this project's build config.

## Commands

```bash
pytest                              # app tests
pytest yahoo_fantasy_api/tests      # vendored library
ruff check fcc tests
fcc doctor                          # post-deploy go/no-go; run it after any change
fcc serve                           # dashboard on 127.0.0.1:8765
cd web && npm run build             # frontend
```

## Invariants — do not break these

**Every platform mutation goes through the action gate** (`fcc/core/actions.py`).
No engine calls a platform write API directly. The gate owns the state machine,
the idempotency key, and the audit row.

**A write that isn't confirmed is not a success.** After submitting, read the
platform back. If the read-back disagrees or can't be performed, the action is
`UNVERIFIED` and notified — never silently counted as done.

**Money-tier actions cannot auto-execute.** FAAB, survivor picks, drops and
trades always require explicit approval. There is deliberately no setting that
changes this, and the HTTP layer is not a way around it. Low tier (pick'em
submits, benching an OUT player) runs unattended.

**Resolve or refuse — never guess.** This recurs everywhere and is the project's
core safety property:

- `fcc/core/teams.py` has no fuzzy matching. "New York" returns `None`, which
  routes to human review.
- `normalize_status()` maps an unknown status to `UNKNOWN`, **never** `ACTIVE`,
  so a new spelling makes the guardian ask rather than start someone.
- `lineup.eligible_positions()` returns `None` for an unrecognised slot, which
  means "don't touch it", not "anything fits".
- The pick'em join sends anything unmatched to a review list.

**Dry run is the default.** `FCC_DRY_RUN=true` means no write leaves the
process. A fresh checkout or a fresh seedbox cannot touch a live league.

**Markets aren't interchangeable.** FantasyGuru staff picks are often against
the spread; pick'em asks who wins outright. An ATS lean on a +7 dog is usually a
straight-up *loss* pick. `engines/pickem.py` converts spread → win probability.
The spread sign convention (negative = favoured) is pinned in the extraction
schema; flipping it silently inverts every pick.

## Gotchas already paid for

- **SQLite discards tzinfo.** All datetime columns use the `UTCDateTime` type
  decorator in `core/models.py`. Without it, `expires_at <= now` raises
  TypeError on a reloaded row — and those comparisons guard the waiver and
  lineup deadlines, so it surfaces at 3am or not at all.
- **Sleeper's `starters` array is positional** against the league's non-bench
  `roster_positions`. Misalign it and every player is labelled with the wrong
  slot while looking correct. `sleeper.build_slots` is a free function so this
  is testable without HTTP.
- **Redaction is default-deny.** An enumerated list of sensitive key names
  leaked `groupId`. Anything id-shaped is redacted unless it's a known-public
  NFL/challenge id. Only the *immediate* parent decides personal scope —
  matching any ancestor made the `entry` wrapper strip the proposition ids the
  fixture exists to show.
- **`/v1/players/nfl` is ~5MB**; Sleeper asks for once-a-day. It's disk-cached.
  The 3am waiver job must not re-download it.

## Secrets

Never commit a credential, never log one, never put one in a commit message or
a test fixture. They live in `data/secrets.enc` (Fernet, key from
`FCC_MASTER_KEY`). Use `fcc secrets set <name>`. `fcc pickem dump` is redacted
by default so fixtures are shareable.

## Style

Match the surrounding code. Comments explain *why* — especially why something
refuses to act. Tests pin failure modes, not just happy paths: the valuable test
is the one asserting the system declined to guess.
