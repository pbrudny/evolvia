# Evolvia: design

*Play, create, evolve.*

Status: draft, 27 Nov 2026. Name and slogan suggested by [rayanhasasneh](https://github.com/rayanhasasneh) (EDP student). Working name before that: Let's Build a Game. Owner: Piotr Brudny. Course: Event Driven Programming (EDP), ATA.

## 1. Summary

Evolvia is a game that its players build. Players play the current version, and once they reach a milestone they can suggest a change ("What if…?"). Other players vote. The best suggestions are built by AI agents, checked automatically, reviewed, and released as a new playable version within minutes. Every version stays playable, so one game grows into a tree of versions and can end up as something completely different from where it started.

It starts one step earlier, with no game at all: players propose game ideas, the AI builds small prototypes of the top ideas, players play them and vote, and the winner becomes the first game.

The system is event driven on purpose. Every step (idea submitted, vote cast, build started, version deployed, XP awarded) is an event, and EDP students can build services that react to those events.

## 2. Goals and non-goals

Goals:
- Players feel they are building the game, not only playing it.
- A suggestion goes live in minutes, without the game breaking.
- Every change is credited to the player whose idea it was.
- Safe for a class: AI-written code can never reach the platform, its secrets, or players' accounts.
- A real event-driven system that students can extend in Phase 3.

Non-goals (for now):
- Open to the public internet. It is for the EDP class.
- Forks on players' GitHub accounts. Everything lives in one repo per game.
- Any automatic link between XP and course grades.
- Big engines (Unity, Godot). Games are small browser games.

## 3. Vocabulary

| Term | Meaning |
|------|---------|
| Platform | The fixed web app: login, idea board, games, voting, builds, XP. Written by people, never changed by AI. |
| Idea | A one-line proposal for a new game ("multiplayer tag where you steal time"). |
| Idea round | A time-boxed round in which players submit and vote on ideas. |
| Prototype | A tiny playable version of one top idea, built by AI from the template. |
| Game | A winning prototype with its own repo, `evolvia-<slug>`. |
| Version | One commit of a game. Playable at its own URL. |
| Suggestion | A "What if…?" change proposed for a specific version of a game. |
| Milestone | A goal inside a game (for example "reach level 3"). Reaching one unlocks suggestions. |
| Constitution | Rules for a game that every AI change must keep (in the game repo, checked by the reviewer). |
| XP | Reputation points earned in the system. Gives small advantages when suggesting. |

## 4. Lifecycle

### 4.1 Idea round (no game yet)

1. Piotr (or a player at Architect level, see §10) opens an idea round with an end time, for example one lecture.
2. Players submit one-line game ideas and upvote others. One idea per player per round.
3. When the round closes, the top 3 ideas go to prototyping.

### 4.2 Prototype round

4. For each top idea, the build pipeline creates a prototype from `evolvia-template`: a few minutes and roughly $1 each.
5. Players play all three prototypes and vote for one. Only players who reached a milestone in a prototype can vote for it.
6. The winner gets its own repo, `evolvia-<slug>`, created from the template, with the prototype as version 0.
7. The losing ideas stay on the idea board and can win a later round.

### 4.3 Evolution loop (a game exists)

8. Players play a version. Reaching a milestone in it unlocks the "What if…?" button for that version.
9. A player writes a suggestion. It is attached to the version they played.
10. Other eligible players vote. The build queue picks the suggestion with the highest priority (§10.3).
11. The build pipeline (§7) turns it into a change on a new branch, checks it, reviews it and commits it.
12. The new version is deployed at its own URL, with an AI-written changelog that names the player whose idea it was.
13. The version joins the game's version tree. Players can play it, suggest changes to it, and vote on whether it becomes the main version (§8.3).

Several idea rounds can run over time, so the class can end up with more than one game.

## 5. Architecture

```
 players (browser)
    │  GitHub login (read:user only)
    ▼
 ┌───────────────────────────── evolvia (platform) ─────────────────────────────┐
 │  web app: idea board · game list · version tree ("Multiverse") · voting · XP │
 │  API + DB (ideas, suggestions, votes, milestones, XP history)                │
 │  publishes / consumes events ──────────► event broker (topic evolvia.*)      │
 └──────────────┬──────────────────────────────────────────────▲────────────────┘
                │ SuggestionAccepted / PrototypeRequested       │ BuildSucceeded / Failed
                ▼                                               │
 ┌──────── build workers (sandbox: no secrets, no network except the git remote) ┐
 │  classifier → builder agent → gates → reviewer agent → commit on branch     │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │ push branch
                ▼
   GitHub: evolvia-<slug> (one repo per game)  ──►  version host (separate origin)
                                                  /g/<slug>/v/<sha>/  static files
                                                  shown in a sandboxed iframe
```

### 5.1 Repos

| Repo | Contents | Who changes it |
|------|----------|----------------|
| `evolvia` | Platform: web app, API, database migrations, build orchestrator, event schemas | People only |
| `evolvia-template` | Starting point for every game: game API, gates, headless bot, constitution, agent prompts | People only |
| `evolvia-<slug>` | One game. Branches per suggestion, `main` = current main version | AI agents (through the pipeline) and people |

All repos are public on GitHub under `pbrudny`, so players can read the code. Secrets live only in the deployment, never in a repo.

### 5.2 Components

- **Web app and API.** GitHub OAuth with the `read:user` scope only: identity, no write access to anyone's account. Idea board, game pages, version tree, suggestion form, voting, XP and levels, admin pages. Suggested stack, matching existing projects (`student-portal`, `ca-quiz`): FastAPI + SQLite on Coolify, with a `/data` volume.
- **Database.** Players, ideas, rounds, games, versions, suggestions, votes, milestones, XP history (append-only).
- **Event broker.** All state changes are published as events (§11). It reuses the EDP broker that Ember Realms already publishes to, under the topic `evolvia.*`.
- **Build workers.** Run the agent pipeline in a sandbox. First version: `claude -p` on a machine Piotr controls, the same pattern as `class_agent` and the Ember Realms loop. Later: containers.
- **Version host.** Serves every version as static files at `/g/<slug>/v/<sha>/` on a **separate origin** (for example `play.evolvia.codewithpeter.com`), embedded in the platform page as a sandboxed iframe. Game code can't read the platform's cookies or call its API as the player.

### 5.3 How the game talks to the platform

The game runs in the iframe and only talks to the platform through `postMessage` with a small fixed set of messages:

| Message | Sent when | Platform does |
|---------|-----------|---------------|
| `session-start` | the game starts | records the start time for this player, game and version |
| `milestone` `{id}` | the player reaches a milestone | checks the minimum time (§6.2), records it, emits `MilestoneReached` |
| `session-end` `{score?}` | the player stops | records play time (used for "players playing your version" XP) |

## 6. The game template

### 6.1 Contract every game must keep

A game is plain JavaScript modules with no build step, so AI changes stay small and readable (the same approach as Ember Realms):

```
game/
  sim.js        create(seed), step(state, inputs), hash(state), milestones  ← all rules
  render.js     draws the state on a canvas, reads input
  main.js       wires sim + render + platform messages
constitution.md rules every change must keep
tests/          rule tests (plain node scripts)
bot.js          headless bot that plays the game
gates.mjs       all automatic checks
```

- `sim.js` is deterministic: a seeded random generator only, no `Math.random()` or `Date` inside the rules. This keeps bot playtests and replays reliable.
- `milestones` lists the goals, each with a minimum time, for example `[{ id: "level3", name: "Reach level 3", minSeconds: 180 }]`.
- Start with single-player or local multiplayer. Online real-time multiplayer is an open question (§15).

### 6.2 Milestone check

Suggesting needs a milestone reached in the same version, or in its parent version.

- The game sends `milestone` when the player reaches it. This is a simple check, which is enough for a class.
- To catch hand-crafted calls, the platform rejects a milestone reached faster than its `minSeconds` after `session-start`.
- One suggestion per player per day, plus votes before anything is built, limit what a faked milestone could do.

### 6.3 Constitution

`constitution.md` in each game repo holds the rules every AI change must keep. The builder gets it with every task and the reviewer checks against it. The template starts with:

1. The game must start and be playable.
2. At least one milestone must stay reachable (checked by the bot gate).
3. `sim.js` stays deterministic.
4. Only the messages in §5.3 may be sent to the platform. No network requests, no external scripts or images.
5. No offensive content, nothing about real people.
6. A change does one thing: the suggestion it was built for.

Players at Curator level can propose changes to a game's constitution. Piotr approves them.

### 6.4 Gates

Plain scripts, no AI. A version is only deployed when all pass:

| Gate | Checks |
|------|--------|
| syntax | every file parses |
| rule tests | `tests/*.mjs` pass |
| determinism | the same seed and inputs give the same `hash(state)` |
| bot playtest | the headless bot plays N games without errors and reaches at least one milestone |
| performance | a step stays under a time budget |
| content policy | no network calls, no external URLs, nothing outside the allowed files |

## 7. Build pipeline

This reuses the multi-agent loop from Ember Realms (`edp-rts/loop/`): agents through `claude -p --agent`, each task in its own git worktree, structured answers checked against a schema.

```
SuggestionAccepted
  → classifier   on topic? safe? small enough? (reject → SuggestionRejected with a reason)
  → builder      writes the change on branch suggestion/<n>-<login> in evolvia-<slug>
  → gates        up to 3 rounds: failures are sent back to the builder
  → reviewer     checks the diff against the suggestion and the constitution (up to 2 rounds)
  → commit       with a "Co-authored-by: <player>" line, so the player is credited on GitHub
  → changelog    AI writes "What changed and whose idea it was" for players
  → deploy       static files to /g/<slug>/v/<sha>/
  → VersionDeployed
```

- The builder may answer `done`, `partial` (works, but not everything was possible; the unmet parts are listed) or `blocked` (nothing worth keeping, with the reason shown to the player).
- Prototypes use the same pipeline with `PrototypeRequested`, starting from `evolvia-template`.
- Rollback: any version can be marked broken (by Piotr, or by Curators with enough votes). Players who open a broken version are sent to its parent.

## 8. Versions

### 8.1 Version tree

Every commit is a version, and a suggestion is always built on the version the player played. So versions form a tree, shown in the game's **Multiverse** view: what changed, who suggested it, how many people play it, and a play button.

### 8.2 URLs

- `/g/<slug>`: the game's main version.
- `/g/<slug>/v/<sha>`: any version.
- `/g/<slug>/tree`: the Multiverse view.

### 8.3 The main version

`main` in the game repo is the version new players get. A version becomes main when:
- Phase 2: Piotr approves it with one click, or
- later: it is the most-voted child of the current main after a set time (for example 24 hours) and has enough players.

Branches that don't become main stay playable. Nothing is deleted.

## 9. Suggestions and voting

- The "What if…?" button opens a short form inside the game page: one sentence, plus an optional longer description.
- Votes: one per player per suggestion, only from players who reached a milestone on that version. No voting for your own suggestion.
- A suggestion expires after 7 days without being built.
- The player can see the status of their suggestion: waiting for votes, queued, building, live, rejected (with the reason).

## 10. XP and levels

### 10.1 Earning XP

Most XP comes from what other players do with your work, so it can't be farmed by clicking.

| Action | XP | Limit |
|--------|----|-------|
| Reach a milestone, first time per version | +5 | once per version |
| Your suggestion gets a vote | +1 per vote | votes from eligible players only |
| Your suggestion goes live | +30 | |
| Players play your version | +1 per unique player | max 50 per version |
| Your idea wins a prototype round | +50 | |
| You report a bug that is confirmed | +10 | |
| Your version is rolled back | −20 | |

### 10.2 Two totals

- **Current XP** loses 20% each month. It decides levels and advantages, so recent activity counts most and new players can catch up.
- **Lifetime XP** never decays. It is kept for Piotr's judgement at the end of the course.

Every award is one row in an append-only history: player, points, reason, game, version, time.

### 10.3 Levels and advantages

| Level | Current XP | Unlocks |
|-------|-----------|---------|
| Player | 0 | play, vote |
| Contributor | 20 | 1 suggestion per day |
| Builder | 100 | 2 per day; your suggestions start with +3 votes |
| Architect | 300 | 3 per day; bigger changes (new mechanics); can open an idea round |
| Curator | 600 | your vote counts double; can flag suggestions and versions; can propose constitution changes |

Every player gets one free suggestion after their first milestone, even before Contributor, so newcomers can always take part.

Build queue priority:

```
priority = eligible votes + level bonus + 0.5 × author current XP (max 30)
```

Ties go to the suggestion that has waited longest.

### 10.4 Course extra points

There is no automatic link between XP and EDP extra points (🏆). At the end of the course Piotr decides how much XP is worth one extra point. To make that easy:
- `/admin/xp.csv` lists each player: GitHub login, student name (matched to the EDP roster through the GitHub login, as in `student-portal`), lifetime XP, suggestions that went live, confirmed bugs.
- The XP history shows how anyone got their points.

## 11. Events

All events go to the broker under `evolvia.<event>`, as JSON with `id`, `type`, `at`, `actor` (GitHub login or `system`) and a `data` object.

| Event | Data |
|-------|------|
| `PlayerJoined` | login |
| `IdeaRoundOpened` / `IdeaRoundClosed` | round id, end time, top ideas |
| `IdeaSubmitted` / `IdeaVoted` | idea id, text |
| `PrototypeRequested` / `PrototypeBuilt` / `PrototypeVoted` | idea id, version url |
| `GameCreated` | slug, repo, version 0 |
| `SessionStarted` / `SessionEnded` | game, version, play time |
| `MilestoneReached` | game, version, milestone id |
| `SuggestionSubmitted` / `SuggestionVoted` | suggestion id, game, base version |
| `SuggestionAccepted` / `SuggestionRejected` | suggestion id, reason |
| `BuildStarted` / `GatesFailed` / `ReviewBlocked` / `BuildSucceeded` / `BuildFailed` | suggestion id, branch, details |
| `VersionDeployed` | game, version, parent, changelog, credited player |
| `VersionPromoted` / `VersionRolledBack` | game, version |
| `XpAwarded` / `LevelReached` | player, points or level, reason |

### 11.1 What students can build (Phase 3)

Each of these only listens to events and needs no change to the platform:
- Slack announcer: new versions, winning ideas, level-ups in `#evolvia`.
- Leaderboards: most-played versions, top contributors.
- Multiverse visualiser: the version tree as a family tree.
- Vote fraud detector: suspicious voting patterns.
- Changelog digest: a daily summary of what changed.
- An XP service that rebuilds the XP history from events, as an exercise in event sourcing.

## 12. Security

- AI-written code runs only in players' browsers, on a separate origin, in a sandboxed iframe with a strict CSP. The platform never loads or runs game code.
- Build workers have no secrets apart from push access to the game repos, and no network apart from the git remote and the model API.
- Player text reaches the builder only after the classifier, and is always marked as player input, never as instructions to the system.
- GitHub login asks for `read:user` only. The platform never holds write access to a player's account.
- The content policy gate blocks network calls and external resources in game code.
- Admin actions (approve, roll back, ban) are logged.

## 13. Cost and limits

Based on the Ember Realms loop: a build costs about $0.50–1.50, and a prototype round of 3 builds about $2–4.

- Builds per day: global cap (for example 20), set in the admin page.
- Suggestions per player per day: by level (§10.3).
- Each build has a time limit and a maximum number of gate and review rounds.

## 14. Phases

**Phase 1: brainstorming (done, lecture 08, 27 Nov).** Idea presented, students sent suggestions (§16).

**Phase 2: MVP (Piotr).**
1. `evolvia-template` with the game contract, gates and bot, plus a first tiny game, built by hand as a test.
2. Platform: GitHub login, game page with iframe, milestones, suggestion form, voting.
3. Build pipeline for suggestions, deploy to the version host, changelog.
4. Version tree and URLs, Piotr approves the main version.
5. XP history and levels.
6. Idea round and prototype round.

**Phase 3: students take part.** Students play, suggest and vote, and build event consumers (§11.1). Pull requests to the game repos are also welcome and go through the same gates and review.

## 15. Open questions

1. **First game:** run a real idea round, or seed one of the students' ideas (for example the time-stealing game from [krustu](https://github.com/krustu)) to have something playable from day one?
2. **Online multiplayer:** real-time multiplayer needs a game server, which means running AI-written code on the server. Option: a small fixed multiplayer relay in the platform that only forwards inputs, with each browser running the same deterministic `sim.js`.
3. **Main version:** when should votes replace Piotr's approval (§8.3)?
4. **Build workers:** keep them on Piotr's machine on the Claude subscription, or move them to containers using the API?
5. **Who else can open idea rounds** besides Piotr and Architects?

## 16. Student suggestions (Phase 1)

Ideas from EDP students, sent by DM on 27 Nov, that shaped this design:

| Student (GitHub) | Idea | Where it is used |
|---------|------|------------------|
| [Mileslov3](https://github.com/Mileslov3) | Preview and quick rollback; the AI agent in a sandbox with no secrets; cost limits per player; a main version plus other versions to keep the class together | §7 rollback, §8.3, §12, §13 |
| [nifemialade](https://github.com/nifemialade) | AI explains what changed and whose suggestion inspired it; version history; conflict checks; AI starts with predefined elements | §7 changelog, §8, §6.3 |
| [muhsinarsu35749](https://github.com/muhsinarsu35749) | "Multiverse" menu to switch between versions; small modular game files | §8.1, §6.1 |
| [saidbro08](https://github.com/saidbro08) | Event-driven flow: `IdeaSubmitted`, a queue and dispatcher, handlers, `NewVersionReady`, top-voted version merges | §7, §8.3, §11 |
| [krustu](https://github.com/krustu) | Time-stealing multiplayer game (60 s each, steal 5 s by touching, traps); players suggest new traps and abilities | §15.1 as a first prototype |
| [Rita-ST](https://github.com/Rita-ST) | Evolution framing (playtime + votes as fitness); suggesting inside the game; trusted players review; a constitution; ancestry view | §6.3, §9, §10.3, §8.1 |
| [rayanhasasneh](https://github.com/rayanhasasneh) | The name **Evolvia** and the slogan *play, create, evolve*; a platform with many games and their versions | title, §4 idea rounds, several games |
| [manvibhatt1137-glitch](https://github.com/manvibhatt1137-glitch) | Sent an image (not yet included) | — |

Four students proposed forks on the player's GitHub. We chose one repo per game instead: branches inside it give the same "many versions", players get credit through `Co-authored-by`, and the platform never needs write access to their accounts.
