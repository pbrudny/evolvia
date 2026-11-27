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
    status TEXT NOT NULL DEFAULT 'open',  -- open | building | live | rejected | expired
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    suggestion INTEGER NOT NULL REFERENCES suggestions(id),
    login TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (suggestion, login)
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


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def emit(conn: sqlite3.Connection, type_: str, actor: str, **data) -> None:
    """Append an event. Names follow the design (DESIGN.md §11): MilestoneReached, SuggestionVoted, ..."""
    conn.execute(
        "INSERT INTO events (type, at, actor, data) VALUES (?, ?, ?, ?)",
        (type_, now(), actor, json.dumps(data, ensure_ascii=False)),
    )
