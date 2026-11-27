"""Idea rounds and prototype rounds (DESIGN.md §4.1–4.2).

A round moves through phases; time-based moves happen lazily in `advance()`, which every round page and every
worker poll calls:

    ideas        players post one-line game ideas (one per round) and upvote others
    prototyping  the top 3 ideas on the board are built from evolvia-template by the build worker
    voting       players play the prototypes and vote for one (only after a milestone in that prototype)
    creating     the winner waits for the worker to create its repo evolvia-<slug>
    closed       the winner is a game (`game` is set), or nothing could be built

The idea board is shared by all rounds: ideas that don't win stay open and can win a later round.
"""

import os
import re
from datetime import UTC, datetime, timedelta

from . import db, xp

PROTOTYPES_REPO = os.environ.get("PROTOTYPES_REPO", "pbrudny/evolvia-prototypes")
GAMES_OWNER = os.environ.get("GAMES_OWNER", "pbrudny")
TOP = 3
PROTOTYPE_WIN = 50
ACTIVE = ("ideas", "prototyping", "voting", "creating", "publishing")

# An idea's votes: weighted (a Curator's vote counts double).
IDEA_VOTES = "(SELECT COALESCE(SUM(weight), 0) FROM idea_votes WHERE idea = i.id)"


def parse(at: str | None) -> datetime | None:
    return datetime.fromisoformat(at) if at else None


def later(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def active(conn):
    return conn.execute(f"SELECT * FROM rounds WHERE status IN {ACTIVE} ORDER BY id DESC LIMIT 1").fetchone()


def can_open(conn, login: str | None, admin: bool) -> bool:
    """Piotr (admins) and players at Architect level or higher (DESIGN.md §10.3)."""
    return bool(login) and (admin or xp.totals(conn, login)["level_no"] >= 4)


def open_round(conn, login: str, title: str, idea_hours: float, vote_hours: float) -> int:
    rid = conn.execute(
        "INSERT INTO rounds (title, status, opened_by, ideas_end, vote_hours, created_at) VALUES (?, 'ideas', ?, ?, ?, ?)",
        (title, login, later(idea_hours), vote_hours, db.now())).lastrowid
    db.emit(conn, "IdeaRoundOpened", login, round=rid, title=title, ideas_end=later(idea_hours))
    return rid


def board(conn, login: str | None = None):
    """Open ideas on the shared board, most votes first."""
    return conn.execute(
        f"SELECT i.*, {IDEA_VOTES} AS votes, EXISTS(SELECT 1 FROM idea_votes WHERE idea = i.id AND login = ?) AS voted "
        "FROM ideas i WHERE i.status = 'open' ORDER BY votes DESC, i.id", (login or "",)).fetchall()


def prototypes(conn, rid: int, login: str | None = None):
    """The ideas prototyped in this round, with their prototype game, version and vote count."""
    return conn.execute(
        "SELECT i.*, g.main_version AS proto_version, "
        "(SELECT COUNT(*) FROM proto_votes WHERE round = ? AND idea = i.id) AS proto_votes, "
        "(SELECT COUNT(DISTINCT login) FROM sessions WHERE version = g.main_version) AS players, "
        "EXISTS(SELECT 1 FROM milestones WHERE login = ? AND version = g.main_version) AS reached "
        "FROM ideas i LEFT JOIN games g ON g.slug = i.proto_game WHERE i.proto_round = ? ORDER BY i.id",
        (rid, login or "", rid)).fetchall()


def close_ideas(conn, r) -> None:
    """End the idea phase: the top 3 open ideas on the board go to prototyping."""
    top = conn.execute(f"SELECT i.*, {IDEA_VOTES} AS votes FROM ideas i WHERE i.status = 'open' "
                       "ORDER BY votes DESC, i.id LIMIT ?", (TOP,)).fetchall()
    if not top:
        conn.execute("UPDATE rounds SET status = 'closed', note = 'No ideas were posted.' WHERE id = ?", (r["id"],))
        db.emit(conn, "IdeaRoundClosed", "system", round=r["id"], top=[])
        return
    for i in top:
        conn.execute("UPDATE ideas SET status = 'queued', proto_round = ?, build_note = 'Waiting for the builder', "
                     "updated_at = ? WHERE id = ?", (r["id"], db.now(), i["id"]))
        db.emit(conn, "PrototypeRequested", "system", round=r["id"], idea=i["id"], title=i["title"], author=i["login"])
    conn.execute("UPDATE rounds SET status = 'prototyping' WHERE id = ?", (r["id"],))
    db.emit(conn, "IdeaRoundClosed", "system", round=r["id"],
            top=[{"idea": i["id"], "title": i["title"], "votes": i["votes"]} for i in top])


def slug_for(conn, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-") or "game"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM games WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def close_voting(conn, r) -> None:
    """End the prototype vote: the most-voted prototype wins (ties: the older idea). The rest go back to the board."""
    built = [p for p in prototypes(conn, r["id"]) if p["status"] == "built"]
    winner = max(built, key=lambda p: (p["proto_votes"], -p["id"])) if built else None
    for p in prototypes(conn, r["id"]):
        if p["status"] in ("built", "failed") and (not winner or p["id"] != winner["id"]):
            conn.execute("UPDATE ideas SET status = 'open', updated_at = ? WHERE id = ?", (db.now(), p["id"]))
    if not winner:
        conn.execute("UPDATE rounds SET status = 'closed', note = 'No prototype could be built.' WHERE id = ?", (r["id"],))
        return
    slug = slug_for(conn, winner["game_name"] or winner["title"])
    conn.execute("UPDATE ideas SET status = 'won', updated_at = ? WHERE id = ?", (db.now(), winner["id"]))
    conn.execute("UPDATE rounds SET status = 'creating', winner = ?, game = ?, note = 'Creating the game repository' "
                 "WHERE id = ?", (winner["id"], slug, r["id"]))
    db.emit(conn, "PrototypeVoted", "system", round=r["id"], winner=winner["id"], votes=winner["proto_votes"], game=slug)
    xp.award(conn, winner["login"], PROTOTYPE_WIN, "prototype", game=winner["proto_game"], version=winner["proto_version"])


def advance(conn) -> None:
    """Move the active round on when its time is up or its prototypes are done."""
    r = active(conn)
    if not r:
        return
    now = datetime.now(UTC)
    if r["status"] == "ideas" and parse(r["ideas_end"]) <= now:
        close_ideas(conn, r)
    elif r["status"] == "prototyping":
        busy = conn.execute("SELECT COUNT(*) FROM ideas WHERE proto_round = ? AND status IN ('queued', 'building')",
                            (r["id"],)).fetchone()[0]
        if not busy:
            if conn.execute("SELECT 1 FROM ideas WHERE proto_round = ? AND status = 'built'", (r["id"],)).fetchone():
                conn.execute("UPDATE rounds SET status = 'voting', vote_end = ? WHERE id = ?", (later(r["vote_hours"]), r["id"]))
            else:
                close_voting(conn, r)
    elif r["status"] == "voting" and parse(r["vote_end"]) <= now:
        close_voting(conn, r)


def force_next(conn, r) -> None:
    """Admin: end the current phase now."""
    if r["status"] == "ideas":
        close_ideas(conn, r)
    elif r["status"] == "voting":
        close_voting(conn, r)
