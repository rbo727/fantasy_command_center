# Handoff to a local session

Written by the remote Claude Code session that built Stages 0–3, for a Claude
Code session running locally in WSL with real credentials and real network
access. Read `CLAUDE.md` first for the invariants; this file is the situational
state.

**Date of handoff:** 2026-09-13, NFL Week 2.
**Branch:** `master` (the feature branch was merged into it; both track together).
**Tests at handoff:** 236 app + 61 vendored library, all passing; ruff clean.

## Start here

```bash
cd ~/Python/fantasy_command_center
git pull
source .venv/bin/activate       # `fcc` only exists while the venv is active
claude                          # CLAUDE.md loads automatically
```

Then: *"Read docs/HANDOFF.md and start on Task 1."*

---

## Why this handoff exists

The remote session's container is egress-restricted: **`gambit-api.fantasy.espn.com`,
`api.sleeper.app` and `fantasyguru.com` are all blocked**, and it holds no
credentials by design. So every platform client was written defensively against
documented-but-unobserved response shapes and verified against *synthetic*
fixtures.

That is the gap you close. You can reach the real APIs and the user's real
credentials are on this machine. **Prefer real data over more fixtures.**

### What is verified vs. assumed

| Component | State |
|---|---|
| Action gate, teams, status normalization, lineup engine, redaction, pick'em join | **Verified.** Pure logic, thoroughly tested. Trust these. |
| FastAPI + dashboard | **Verified by running it** — server served the built UI and a real browser click moved an action to `approved` in the DB. |
| Sleeper connector | **Live-verified.** League/team resolve, and slot alignment, FAAB remaining, and injury-status mapping all confirmed against the app (Task 1, 2026-09-14). |
| ESPN pick'em *reads* | **Assumed.** Parsers try several field spellings because the real ones were never observed. |
| ESPN pick'em *writes* | **Unknown.** ESPN publishes no submission endpoint. Handled by replaying a request the user captures in DevTools. |
| FantasyGuru login + extraction | **Assumed.** Login selectors are a guess; the page was never loaded. |

---

## Task 1 — Finish verifying Sleeper (start here)

Already confirmed live: `fcc doctor` passes, and the Leagues tab shows the right
league and team. What remains is the part that fails *silently*.

```bash
fcc serve        # Leagues tab → expand Roster
```

Open the Sleeper app beside it and confirm:

- Do the starters land in the **right slots**? `build_slots` aligns `starters`
  positionally against non-bench `roster_positions`; this is the highest-risk
  assumption in the connector.
- Is `faab_remaining` right? It's `waiver_budget - waiver_budget_used`.
- Do injury statuses map sensibly? Anything showing `unknown` means Sleeper used
  a spelling absent from `_STATUS_ALIASES` in `fcc/platforms/base.py` — add it,
  and add a test case. **Do not** let an unmapped status default to `ACTIVE`.

Then `fcc lineup check` and sanity-check the plan by eye.

## Task 2 — Tighten the ESPN pick'em parsers with a real payload

```bash
fcc secrets set espn_swid && fcc secrets set espn_s2   # from DevTools cookies
fcc pickem dump espn_pickem --week 2 --raw             # local only; --raw is unredacted
```

Compare against `fcc/platforms/espn_pickem.py`:

- `parse_propositions` guesses at `propositions` / `props` / nested
  `events[].propositions`. **Replace the guesswork with what ESPN actually
  returns**, and save a redacted dump as a test fixture.
- `Option.team_code` comes from running `abbrev`/`name` through
  `teams.normalize()`. If any option fails to resolve, add the alias to
  `fcc/core/teams.py` — do not loosen `normalize()` into fuzzy matching.
- `existing_picks()` has a known ESPN quirk: an entry may report picks as
  submitted while withholding the selections until kickoff. Confirm whether that
  holds for this challenge; the verifier currently treats an empty read-back as
  *inconclusive*, not as failure.

## Task 3 — Wire the pick'em write path

The user captures the submit request (DevTools → Network → filter `gambit` →
make a pick → Copy as cURL), then:

```bash
fcc pickem capture-write --curl-file /tmp/capture.txt
fcc pickem run espn_pickem --week 2 --picks-file picks.json   # preview
```

`fcc/platforms/curl_import.py` strips Cookie/Authorization headers rather than
persisting them, and detects the pick array and its field spellings from the
capture. If detection fails it raises with guidance — improve the detection
rather than hardcoding ESPN's field names.

Only then set `FCC_DRY_RUN=false` and submit for real, and **check the ESPN UI
afterwards** — the verifier's read-back is not a substitute for looking once.

## Task 4 — FantasyGuru

`fcc/sources/fantasyguru.py` is split so the fuzzy part is testable:
`extract_staff_picks(html, ...)` takes HTML and never touches the network.

The Playwright login is the fragile half. Selectors are deliberately broad. If
login fails, the fetched page is cached at `data/cache/fantasyguru/*.html` —
diagnose from the cache, don't re-fetch in a loop against a subscriber site.

When it works, check the extraction's **spread signs** against the page by hand
at least once. A flipped sign silently inverts every downstream pick, and the
tests can only pin the convention, not whether the model read the page right.

## Task 5 — Stage 4 (the write connectors)

The **FAAB bid model** (`fcc/engines/faab.py`) already exists and is tested —
pure logic, so it was buildable without network access. It is not yet wired to
anything: nothing calls `parse_winning_bids` against a real transaction log, and
no command turns a recommendation into a `WAIVER_CLAIM` proposal. Wiring it is a
good first Stage 4 task, and Sleeper's transaction history
(`/v1/league/<id>/transactions/<week>`) is readable without auth, so the
calibration half can be verified immediately.

Watch for: the model returns `None` when there is no projection. Route that to
review — do not substitute a default bid.


Stage 3's guardian currently detects but cannot act: `--submit` records the plan
and says so. Unblocking it means:

- **Yahoo**: `change_positions` and `claim_and_drop_players(faab=)` are already
  available through the merged vendored library. Needs OAuth app registration
  and a browser consent flow — a local session can do this; the remote one
  could not.
- **Sleeper**: authenticated GraphQL with a bearer token captured from DevTools.
  See `cameron-eth/sleeper-sdk` for the shape.
- Then the 3am Wednesday waiver sniper, which is the highest-stakes automation
  in the project. Dry-run it against a past waiver Wednesday before it ever runs
  live, and make its first live claim one the user doesn't actually need.

---

## Things to be careful about

**Don't relax a refusal to make a test pass.** Several components deliberately
decline to act: `normalize()` on "New York", `eligible_positions()` on an
unknown slot, the pick'em review list. Those refusals *are* the feature. If one
fires too often, add the missing alias or slot rule — don't add a fallback that
guesses.

**Don't edit `yahoo_fantasy_api/`.** It's vendored upstream. Changes there
conflict on every future `git merge upstream/master`.

**Coordinate on the branch.** The remote session may also be pushing to
`claude/fantasy-football-manager-crzt3h`. Pull before starting and push when
done; don't both hold uncommitted work on it.

**Credentials never leave the machine.** Not into commit messages, test
fixtures, logs, or chat. `fcc pickem dump` is redacted by default; `--raw` is
local-only.

**The season is live.** Pick'em locks Thursday evening; waivers clear 3am
Wednesday. A half-finished automation that submits something wrong is worse than
one that does nothing, which is why `FCC_DRY_RUN` defaults to on — leave it on
until you've watched a run you believe.

## Definition of done for this handoff

- [x] Sleeper roster verified against the app, slot-by-slot
- [ ] Real ESPN dump captured; parsers match the real payload; fixture committed
- [ ] Pick'em write path captured and a preview run produces sensible picks
- [ ] FantasyGuru extraction verified, spread signs checked by hand
- [ ] Any new status spellings / team aliases / slot types added **with tests**
- [ ] `pytest && ruff check fcc tests && fcc doctor` all clean
