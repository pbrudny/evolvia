"""XP and levels (DESIGN.md §10). Every award is one row in the append-only `xp` table.

Current XP loses 20% each month (smoothly: points × 0.8^(days / 30)); lifetime XP never decays.
"""

from datetime import UTC, datetime

from . import db

MILESTONE, VOTE, LIVE, PLAYER = 5, 1, 30, 1
PLAYERS_MAX_PER_VERSION = 50
DECAY_PER_MONTH = 0.8

# (name, current XP needed, suggestions per day, bonus votes on own ideas, vote weight)
LEVELS = [
    ("Player", 0, 1, 0, 1),
    ("Contributor", 20, 1, 0, 1),
    ("Builder", 100, 2, 3, 1),
    ("Architect", 300, 3, 3, 1),
    ("Curator", 600, 3, 3, 2),
]
UNLOCKS = {
    "Player": "play, vote, 1 idea per day after your first milestone",
    "Contributor": "1 idea per day",
    "Builder": "2 ideas per day, your ideas start with +3 votes",
    "Architect": "3 ideas per day, bigger changes (new mechanics)",
    "Curator": "3 ideas per day, your vote counts double",
}


def decayed(points: int, at: str, when: datetime | None = None) -> float:
    days = max(0.0, ((when or datetime.now(UTC)) - datetime.fromisoformat(at)).total_seconds() / 86400)
    return points * DECAY_PER_MONTH ** (days / 30)


def totals(conn, login: str) -> dict:
    rows = conn.execute("SELECT points, at FROM xp WHERE login = ?", (login,)).fetchall()
    current = max(0, round(sum(decayed(r["points"], r["at"]) for r in rows)))
    lifetime = sum(r["points"] for r in rows)
    return {"current": current, "lifetime": lifetime, **level_info(current)}


def level_info(current: int) -> dict:
    i = max(i for i, lv in enumerate(LEVELS) if current >= lv[1])
    name, _, per_day, bonus, weight = LEVELS[i]
    nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return {"level": name, "level_no": i + 1, "per_day": per_day, "bonus_votes": bonus, "vote_weight": weight,
            "next_level": nxt[0] if nxt else None, "next_at": nxt[1] if nxt else None, "unlocks": UNLOCKS[name]}


def award(conn, login: str | None, points: int, reason: str, *, game: str | None = None,
          version: str | None = None, actor: str = "system") -> None:
    """Add one XP row, emit XpAwarded, and LevelReached when the player's level went up."""
    if not login or not points:
        return
    before = totals(conn, login)["level_no"]
    conn.execute("INSERT INTO xp (login, points, reason, game, version, at) VALUES (?, ?, ?, ?, ?, ?)",
                 (login, points, reason, game, version, db.now()))
    db.emit(conn, "XpAwarded", actor, player=login, points=points, reason=reason, game=game, version=version)
    after = totals(conn, login)
    if after["level_no"] > before:
        db.emit(conn, "LevelReached", actor, player=login, level=after["level"], current_xp=after["current"])


def award_player(conn, version: str, player: str) -> None:
    """A new unique player on a version gives its credited author +1, at most 50 per version."""
    v = conn.execute("SELECT game, credited_login FROM versions WHERE id = ?", (version,)).fetchone()
    author = v["credited_login"] if v else None
    if not author or author == player:
        return
    given = conn.execute("SELECT COUNT(*) FROM xp WHERE login = ? AND version = ? AND reason = 'players'",
                         (author, version)).fetchone()[0]
    if given < PLAYERS_MAX_PER_VERSION:
        award(conn, author, PLAYER, "players", game=v["game"], version=version)
