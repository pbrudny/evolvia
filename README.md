# Evolvia

*Play, create, evolve.*

Evolvia is a game that its players build. Play the current version, reach a milestone, and suggest a change ("What if…?"). Other players vote, AI agents build the best suggestions, automatic checks and a review make sure the game still works, and a few minutes later there is a new version to play. Every version stays playable, so the game grows into a tree of versions and can turn into something completely different.

It starts with no game at all: players propose game ideas, the AI builds small prototypes of the top ideas, everyone plays them and votes, and the winner becomes the first game.

A class project for Event Driven Programming at ATA. Every step is an event, so students can build their own services on top of it.

## Status

Design phase. Read the design: [DESIGN.md](DESIGN.md).

| Phase | What | Status |
|-------|------|--------|
| 1 | Brainstorming with the class | done, 27 Nov 2026 |
| 2 | MVP: game template, platform, build pipeline | next |
| 3 | Students play, suggest, vote and build event consumers | later |

## Repos

| Repo | What |
|------|------|
| `evolvia` (this one) | The platform: login, idea board, voting, builds, XP |
| `evolvia-template` | Starting point for every game (not created yet) |
| `evolvia-<slug>` | One repo per game (none yet) |

## Credits

Name and slogan by [rayanhasasneh](https://github.com/rayanhasasneh). The ideas from EDP students that shaped the design are listed in [DESIGN.md §16](DESIGN.md#16-student-suggestions-phase-1).
