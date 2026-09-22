# Fantasy Command Center — setup

Local development on your home PC. Deployment to the seedbox is Stage 6 and is
covered in `docs/DEPLOY.md` once we get there.

## 1. Install

```bash
git clone https://github.com/rbo727/fantasy_command_center
cd fantasy_command_center

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the `fcc` command. (The repository was renamed from
`yahoo_fantasy_api`; if you cloned it under the old name, GitHub's redirect
keeps everything working — run
`git remote set-url origin https://github.com/rbo727/fantasy_command_center`
to point at the new URL.)

### Coming from the `ffm` naming

The package, command and environment prefix were `ffm` / `FFM_` early on. If you
already set things up under those names:

```bash
sed -i 's/^FFM_/FCC_/' .env      # FFM_MASTER_KEY -> FCC_MASTER_KEY, etc.
mv data/ffm.db data/fcc.db       # only if you have one
pip uninstall ffm                # then reinstall as above
```

The encrypted secret store (`data/secrets.enc`) is unaffected — same file, same
master key value, only the variable holding it is renamed. `fcc doctor` will
tell you if anything is still missing.

## 2. Master key and secrets

```bash
fcc secrets init            # prints FCC_MASTER_KEY — put it in .env
cp .env.example .env        # then paste the key in
```

The key is never stored by fcc. Keep a copy in your password manager: lose it
and the secret store has to be rebuilt.

## 3. ESPN cookies

In a browser logged into ESPN, DevTools → Application → Cookies →
`https://fantasy.espn.com`. Copy `SWID` (keep the braces) and `espn_s2`.

```bash
fcc secrets set espn_swid   # prompts without echoing
fcc secrets set espn_s2
```

These expire every few months. When pick'em jobs start failing with HTTP 401,
this is why — re-copy them and `fcc doctor` will go green again.

## 4. Leagues

```bash
cp config/leagues.example.yml config/leagues.yml
```

The pick'em `challenge` slug is in the URL when you make picks:
`fantasy.espn.com/games/`**`nfl-pickem-2026`**`/make-picks`.

## 5. Check the host

```bash
fcc doctor
```

Run this after every change and before trusting any unattended job. It is the
fastest way to find out that a cookie expired while you weren't looking.

## 6. Teach fcc how to submit picks (one-time, ~2 minutes)

ESPN does not publish its pick-submission endpoint, so rather than guessing a
payload, fcc replays a request you captured.

1. Open your pick'em entry.
2. DevTools → Network tab, filter for `gambit`.
3. Make one pick and submit it.
4. Right-click the resulting `POST`/`PUT` → **Copy as cURL**.
5. Paste it into a file, then:

```bash
fcc pickem capture-write --curl-file /tmp/capture.txt
```

fcc prints exactly what it detected — the URL, the field names, and which
credential headers it dropped. Cookies and `Authorization` headers are
deliberately **not** saved; fcc sends your stored ESPN cookies instead.

Also worth doing once, so the parsers can be checked against ESPN's real field
names rather than assumed ones:

```bash
fcc pickem dump espn_pickem --week 1
```

The dump is **redacted by default** and safe to share. Your ESPN account id,
entry id, display name, group name and other entrants' names are replaced with
stable pseudonyms; structure, key names, types, proposition/outcome ids and all
NFL content are preserved, so it remains a valid fixture. The command prints
exactly which paths it redacted.

Identifiers are handled default-deny: anything id-shaped is replaced unless it
is a known-public NFL or challenge id, so a field ESPN adds later gets redacted
rather than leaked. Skim the file before sending it anywhere regardless — only
you can recognise something of yours in an unexpected field.

`--raw` writes the unredacted response instead. Keep those local.

## 7. Run pick'em

```bash
# Preview — matches picks to the slate, submits nothing
fcc pickem run espn_pickem --week 1 --picks-file picks.json

# Submit (still blocked unless FCC_DRY_RUN=false)
fcc pickem run espn_pickem --week 1 --picks-file picks.json --submit
```

With no `--picks-file`, fcc logs into FantasyGuru, fetches the staff-picks page
from `extra.fantasyguru_url` (or `--fg-url`), and structures it with one Claude
call. Store the credentials first:

```bash
fcc secrets set fantasyguru_username
fcc secrets set fantasyguru_password
fcc secrets set anthropic_api_key
```

The fetched page is cached for 30 minutes and the raw HTML is kept under
`data/cache/fantasyguru/`, so a parsing problem never costs a re-fetch — and
when the site is redesigned mid-season, the cached page is the evidence. Use
`--refresh` to bypass the cache.

`--picks-file` bypasses FantasyGuru entirely and is the fastest way to test the
rest of the chain:

```json
[
  {"matchup": "BAL@KC",  "team": "Chiefs",  "market": "ATS", "spread": -6.5, "conviction": 4},
  {"matchup": "SF@SEA",  "team": "49ers",   "market": "ML",  "spread": -3.0, "conviction": 3}
]
```

`market` is one of `ATS`, `ML`, `SU`, `OU`. **This field matters.** An ATS lean
on a +7 underdog is not a pick for them to win — fcc translates it to the
straight-up favourite and tells you it did. See `fcc/engines/pickem.py`.

Anything fcc can't resolve confidently is listed under "Needs review" and is
never submitted.

## Safety model

- `FCC_DRY_RUN=true` (the default) means no write leaves the process. The exact
  payload is recorded so you can inspect it.
- Pick'em submits and benching an inactive player run unattended.
- FAAB bids, survivor picks, drops and trades always need your approval.
- Every write is a row in the `actions` table, and is read back from the
  platform afterwards. A write that isn't confirmed is recorded `unverified`
  and notified — never silently counted as success.

## 8. The dashboard

```bash
cd web && npm install && npm run build && cd ..
fcc serve
```

Then open <http://127.0.0.1:8765>.

`fcc serve` binds to **localhost by default and should stay that way**. It can
change your lineups and spend FAAB, and it has no authentication of its own —
reach it from the seedbox over an SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-seedbox
```

Three tabs:

- **Overview** — what needs a decision. Money-tier actions waiting on you, plus
  any write that failed or came back unverified. This is the tab that matters.
- **Leagues** — record, week, FAAB remaining, waiver position, and the roster
  with injury status per slot. Unavailable starters are highlighted.
- **Waivers** — what winning a waiver has actually cost in your league, from its
  own transaction log: median winning bid, median runner-up, how often claims are
  contested, and a price ladder ("$31 would have won 80% of comparable claims").
  Filterable by position. For a league with `format: guillotine`, it also shows
  every surviving team's remaining FAAB and the price that wins outright.
- **Action log** — every mutation the app has proposed or made.

A league that can't be read shows its error on its own card; the rest of the
page still works. A platform that isn't wired up yet is shown differently from
one that is broken, so "Stage 3 hasn't happened" never looks like an outage.

For front-end work, `npm run dev` in `web/` gives hot reload and proxies `/api`
to `fcc serve` on port 8765.

## 9. Lineup guardian

```bash
fcc lineup check                 # every league
fcc lineup check sleeper_main --week 5
```

It finds starters who **definitely** cannot play — OUT, IR, suspended, PUP,
inactive, or on bye — and names the best eligible bench replacement. Exit code
is non-zero when anything needs attention, so a cron wrapper can alert on it.

What it deliberately will **not** do:

- swap a **Questionable** starter (benching someone who then plays is its own
  loss) — it warns instead
- fill a slot type it doesn't recognise, since guessing a slot's rules produces
  illegal lineups that look correct
- use one bench player for two holes
- start anyone unavailable, including a bench player on bye

`--submit` currently records the swaps without sending them: no platform write
connector exists yet. Yahoo has `change_positions` available through the merged
library and Sleeper needs its authenticated GraphQL path — both land in Stage 4.
Until then the guardian is an alarm, not an autopilot.
