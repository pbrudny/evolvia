# Evolvia

*Play, create, evolve.*

Evolvia is a game that its players build. Play the current version, reach a milestone, and suggest a change ("What if…?"). Other players vote, AI agents build the best suggestions, automatic checks and a review make sure the game still works, and a few minutes later there is a new version to play. Every version stays playable, so the game grows into a tree of versions and can turn into something completely different.

![One suggestion's trip through Evolvia: people play, suggest and vote; AI builds, checks and releases](docs/evolvia-loop.png)

It starts with no game at all: players propose game ideas, the AI builds small prototypes of the top ideas, everyone plays them and votes, and the winner becomes the first game.

A class project for Event Driven Programming at ATA. Every step is an event, so students can build their own services on top of it.

## Status

**Live:** https://evolvia.atabuilderslab.pl (sign in with GitHub to play). Design: [DESIGN.md](DESIGN.md).

| Phase | What | Status |
|-------|------|--------|
| 1 | Brainstorming with the class | done, 27 Nov 2026 |
| 2 | MVP: game template ✓, platform ✓ (login, milestones, ideas, votes), build pipeline | in progress |
| 3 | Students play, suggest, vote and build event consumers | later |

## Repos

| Repo | What |
|------|------|
| `evolvia` (this one) | The platform: login, games, milestones, ideas, votes, event feed (builds and XP next) |
| [`evolvia-template`](https://github.com/pbrudny/evolvia-template) | Starting point for every game: rules contract, sample game, bot, gates, constitution |
| `evolvia-<slug>` | One repo per game (none yet) |

## The platform

FastAPI + SQLite, one app on two addresses:

- **https://evolvia.atabuilderslab.pl**: pages, GitHub login, the API, `/api/events` (the public event feed).
- **https://evolvia-play.atabuilderslab.pl**: only the files of game versions, shown in a sandboxed frame. No cookies, strict CSP, and the game page needs a short-lived ticket that the platform gives to signed-in players.

```bash
uv sync
uv run pytest                                   # tests (no network)
DATA_DIR=./data uv run uvicorn evolvia.server:app --port 8000
# then open http://127.0.0.1:8000 (platform) — the play address defaults to http://localhost:8000
```

Admin (on the server: `docker exec <container> evolvia-admin ...`):

```bash
evolvia-admin add-game --game spark-run --name "Spark Run" --repo pbrudny/evolvia-template
evolvia-admin add-version --game spark-run --ref <branch|sha> --changelog "..." --credit <login> [--main]
evolvia-admin set-main --game spark-run --version spark-run-abc1234
evolvia-admin list
```

Settings: `BASE_URL`, `PLAY_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SESSION_SECRET`, `ADMIN_LOGINS`, `SUGGESTIONS_PER_DAY` (default 1), `DATA_DIR` (default `/data`).

## Credits

Name and slogan by [rayanhasasneh](https://github.com/rayanhasasneh). The ideas from EDP students that shaped the design are listed in [DESIGN.md §16](DESIGN.md#16-student-suggestions-phase-1).
