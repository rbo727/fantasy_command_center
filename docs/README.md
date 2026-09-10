# Documentation

This directory holds documentation for **two** different things, because the
repository vendors the Yahoo API library it depends on. Read the table before
following any guide here.

## Fantasy Command Center (this project)

| Document | What it covers |
|---|---|
| [SETUP.md](SETUP.md) | Install, credentials, league config, and running ESPN pick'em. **Start here.** |
| `DEPLOY.md` | Seedbox deployment. Written in Stage 6; not present yet. |
| `RUNBOOK.md` | Mid-season breakage. Written in Stage 6; not present yet. |

The design rules — the action gate, read-back verification, the money-tier
approval requirement — are in the [top-level README](../README.md).

## The vendored `yahoo_fantasy_api` library (not this project)

`index.rst`, `introduction.rst`, `authentication.rst`, `modules.rst`,
`yahoo_fantasy_api.rst`, `conf.py` and `Makefile` are the Sphinx documentation
for [spilchen/yahoo_fantasy_api](https://github.com/spilchen/yahoo_fantasy_api),
the Yahoo Fantasy API bindings vendored in `yahoo_fantasy_api/`.

They describe that library's API, not Fantasy Command Center, and they still say
"yahoo_fantasy_api" throughout — correctly, since that is the library's name.
They are deliberately left untouched so `git merge upstream/master` stays
conflict-free.

Useful when you're working on the Yahoo integration (Stages 3–4) and want the
library's own reference: <https://yahoo-fantasy-api.readthedocs.io/>.
