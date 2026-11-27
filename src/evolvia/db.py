"""SQLite storage. Every state change is also written to `events`, the event stream students build on."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    login TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo TEXT NOT NULL,
    main_version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,              -- '<game>-<short sha>'
    game TEXT NOT NULL REFERENCES games(slug),
    sha TEXT NOT NULL,
    parent TEXT REFERENCES versions(id),
    milestones TEXT NOT NULL,         -- JSON copy of the version's milestones.json
    changelog TEXT NOT NULL DEFAULT '',
    credited_login TEXT,              -- the player whose idea it was
    suggestion_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    version TEXT NOT NULL REFERENCES versions(id),
    seed INTEGER,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    score INTEGER,
    seconds INTEGER
);
CREATE TABLE IF NOT EXISTS milestones (
    login TEXT NOT NULL,
    version TEXT NOT NULL REFERENCES versions(id),
    milestone TEXT NOT NULL,
    session INTEGER NOT NULL REFERENCES sessions(id),
    seconds INTEGER NOT NULL,
    reached_at TEXT NOT NULL,
    PRIMARY KEY (login, version, milestone)
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL REFERENCES games(slug),
    base_version TEXT NOT NULL REFERENCES versions(id),
    login TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',  -- open | accepted | building | live | rejected | failed
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    suggestion INTEGER NOT NULL REFERENCES suggestions(id),
    login TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (suggestion, login)
);
CREATE TABLE IF NOT EXISTS xp (           -- append-only XP history (DESIGN.md §10)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,             -- milestone | vote | live | players | bug | prototype | rollback | manual
    game TEXT,
    version TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS xp_login ON xp (login);
CREATE TABLE IF NOT EXISTS rounds (       -- idea and prototype rounds (DESIGN.md §4.1-4.2), see rounds.py
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL,             -- ideas | prototyping | voting | creating | publishing | closed
    opened_by TEXT NOT NULL,
    ideas_end TEXT NOT NULL,
    vote_hours REAL NOT NULL,
    vote_end TEXT,
    winner INTEGER,                   -- ideas(id)
    game TEXT,                        -- slug of the game made from the winner
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ideas (        -- the shared idea board: one-line game ideas
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round INTEGER NOT NULL REFERENCES rounds(id),   -- the round it was posted in
    login TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',  -- open | queued | building | built | failed | rejected | won
    proto_round INTEGER,              -- the round it was prototyped in
    proto_game TEXT,                  -- slug of its prototype (games.kind = 'prototype')
    game_name TEXT,                   -- name the builder gave the game
    build_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS idea_votes (
    idea INTEGER NOT NULL REFERENCES ideas(id),
    login TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (idea, login)
);
CREATE TABLE IF NOT EXISTS proto_votes (   -- one vote per player per round, for a prototype they reached a milestone in
    round INTEGER NOT NULL REFERENCES rounds(id),
    login TEXT NOT NULL,
    idea INTEGER NOT NULL REFERENCES ideas(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (round, login)
);
CREATE TABLE IF NOT EXISTS merges (       -- several versions combined into one (Multiverse "Merge")
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL REFERENCES games(slug),
    versions TEXT NOT NULL,           -- JSON list of version ids; the first is the base (the new version's parent)
    login TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | building | live | failed
    build_note TEXT NOT NULL DEFAULT '',
    result_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "evolvia.db", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


MIGRATIONS = [
    # (table, column, definition) — added after the first deploy
    ("suggestions", "build_note", "TEXT NOT NULL DEFAULT ''"),
    ("suggestions", "result_version", "TEXT"),
    ("suggestions", "updated_at", "TEXT"),
    ("suggestions", "bonus_votes", "INTEGER NOT NULL DEFAULT 0"),  # Builder level and up: +3
    ("votes", "weight", "INTEGER NOT NULL DEFAULT 1"),              # Curator: 2
    ("games", "kind", "TEXT NOT NULL DEFAULT 'game'"),             # game | prototype
    ("games", "round", "INTEGER"),                                  # prototypes: their round
    ("versions", "merged_from", "TEXT"),                            # JSON list of version ids for merged versions
]


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def emit(conn: sqlite3.Connection, type_: str, actor: str, **data) -> None:
    """Append an event. Names follow the design (DESIGN.md §11): MilestoneReached, SuggestionVoted, ..."""
    conn.execute(
        "INSERT INTO events (type, at, actor, data) VALUES (?, ?, ?, ?)",
        (type_, now(), actor, json.dumps(data, ensure_ascii=False)),
    )
