# Setup

Local development on your home PC. Deployment to the seedbox is Stage 6 and is
covered in `docs/DEPLOY.md` once we get there.

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Master key and secrets

```bash
ffm secrets init            # prints FFM_MASTER_KEY — put it in .env
cp .env.example .env        # then paste the key in
```

The key is never stored by ffm. Keep a copy in your password manager: lose it
and the secret store has to be rebuilt.

## 3. ESPN cookies

In a browser logged into ESPN, DevTools → Application → Cookies →
`https://fantasy.espn.com`. Copy `SWID` (keep the braces) and `espn_s2`.

```bash
ffm secrets set espn_swid   # prompts without echoing
ffm secrets set espn_s2
```

These expire every few months. When pick'em jobs start failing with HTTP 401,
this is why — re-copy them and `ffm doctor` will go green again.

## 4. Leagues

```bash
cp config/leagues.example.yml config/leagues.yml
```

The pick'em `challenge` slug is in the URL when you make picks:
`fantasy.espn.com/games/`**`nfl-pickem-2026`**`/make-picks`.

## 5. Check the host

```bash
ffm doctor
```

Run this after every change and before trusting any unattended job. It is the
fastest way to find out that a cookie expired while you weren't looking.

## 6. Teach ffm how to submit picks (one-time, ~2 minutes)

ESPN does not publish its pick-submission endpoint, so rather than guessing a
payload, ffm replays a request you captured.

1. Open your pick'em entry.
2. DevTools → Network tab, filter for `gambit`.
3. Make one pick and submit it.
4. Right-click the resulting `POST`/`PUT` → **Copy as cURL**.
5. Paste it into a file, then:

```bash
ffm pickem capture-write --curl-file /tmp/capture.txt
```

ffm prints exactly what it detected — the URL, the field names, and which
credential headers it dropped. Cookies and `Authorization` headers are
deliberately **not** saved; ffm sends your stored ESPN cookies instead.

Also worth doing once, so the parsers can be checked against ESPN's real field
names rather than assumed ones:

```bash
ffm pickem dump espn_pickem --week 1
```

## 7. Run pick'em

```bash
# Preview — matches picks to the slate, submits nothing
ffm pickem run espn_pickem --week 1 --picks-file picks.json

# Submit (still blocked unless FFM_DRY_RUN=false)
ffm pickem run espn_pickem --week 1 --picks-file picks.json --submit
```

With no `--picks-file`, ffm logs into FantasyGuru, fetches the staff-picks page
from `extra.fantasyguru_url` (or `--fg-url`), and structures it with one Claude
call. Store the credentials first:

```bash
ffm secrets set fantasyguru_username
ffm secrets set fantasyguru_password
ffm secrets set anthropic_api_key
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
on a +7 underdog is not a pick for them to win — ffm translates it to the
straight-up favourite and tells you it did. See `ffm/engines/pickem.py`.

Anything ffm can't resolve confidently is listed under "Needs review" and is
never submitted.

## Safety model

- `FFM_DRY_RUN=true` (the default) means no write leaves the process. The exact
  payload is recorded so you can inspect it.
- Pick'em submits and benching an inactive player run unattended.
- FAAB bids, survivor picks, drops and trades always need your approval.
- Every write is a row in the `actions` table, and is read back from the
  platform afterwards. A write that isn't confirmed is recorded `unverified`
  and notified — never silently counted as success.
