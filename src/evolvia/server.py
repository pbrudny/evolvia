"""Evolvia platform: GitHub login, games in a sandboxed frame, milestones, "What if…?" suggestions and votes.

One app, two addresses:
- BASE_URL (evolvia.atabuilderslab.pl): pages, login, API. Has the session cookie.
- PLAY_URL (evolvia-play.atabuilderslab.pl): only the static files of game versions. No cookies, strict CSP,
  shown inside a sandboxed iframe, so AI-written game code can never reach the platform or the player's session.
"""

import csv
import hmac
import io
import json
import mimetypes
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from . import db, xp
from .versions import install, version_dir

CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
PLAY_URL = os.environ.get("PLAY_URL", "http://localhost:8000").rstrip("/")
PLAY_HOST = urlsplit(PLAY_URL).netloc
ADMINS = {a.strip().lower() for a in os.environ.get("ADMIN_LOGINS", "").split(",") if a.strip()}
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
# Claude picks ideas by itself: the top-voted open idea with at least AUTO_BUILD_MIN_VOTES votes, at most
# AUTO_BUILDS_PER_DAY automatic builds per game. 0 votes = automatic picking off (admins accept by hand).
AUTO_BUILD_MIN_VOTES = int(os.environ.get("AUTO_BUILD_MIN_VOTES", "2"))
AUTO_BUILDS_PER_DAY = int(os.environ.get("AUTO_BUILDS_PER_DAY", "5"))
SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
# Only signed-in players get a play link: the game page is opened with a short-lived signed ticket.
TICKETS = URLSafeTimedSerializer(SECRET, salt="play-ticket")
TICKET_MAX_AGE = 12 * 3600

db.init()
HERE = Path(__file__).parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- the play address: static game files only ----------

@app.middleware("http")
async def split_hosts(request: Request, call_next):
    host = request.headers.get("host", "")
    if host == PLAY_HOST:
        return serve_game_file(request)
    response = await call_next(request)
    h = response.headers
    h.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        f"frame-src {PLAY_URL}; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    return response


def serve_game_file(request: Request) -> Response:
    """GET /v/<version>/<path> on the play host. Nothing else exists there; no cookies are ever set."""
    parts = request.url.path.strip("/").split("/", 2)
    if request.method not in ("GET", "HEAD") or len(parts) < 2 or parts[0] != "v":
        return Response("Not found", 404)
    vid, rel = parts[1], (parts[2] if len(parts) == 3 else "index.html")
    base = version_dir(vid).resolve()
    path = (base / rel).resolve()
    if not str(path).startswith(str(base) + os.sep) or not path.is_file():
        return Response("Not found", 404)
    if rel == "index.html":  # the entry page needs a ticket the platform gives only to signed-in players
        try:
            if TICKETS.loads(request.query_params.get("t", ""), max_age=TICKET_MAX_AGE) != vid:
                raise BadSignature("wrong version")
        except BadSignature:
            return Response("Sign in on Evolvia to play.", 403)
    headers = {
        # The frame is sandboxed without allow-same-origin, so its origin is opaque: module scripts need CORS.
        "Access-Control-Allow-Origin": "*",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors {BASE_URL}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=3600",
    }
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".js":
        media = "text/javascript"
    return FileResponse(path, media_type=media, headers=headers)


# Sessions: the play host never touches request.session, so no cookie is ever set there (and the browser
# never sends the platform cookie to a different host).
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    https_only=BASE_URL.startswith("https"),
    same_site="lax",
    max_age=30 * 24 * 3600,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ---------- helpers ----------

def me(request: Request) -> str | None:
    return request.session.get("login")


def need_login(request: Request) -> str:
    login = me(request)
    if not login:
        raise HTTPException(401, "Sign in with GitHub first")
    return login


def is_admin(login: str | None) -> bool:
    return bool(login) and login.lower() in ADMINS


def version_row(conn, vid: str):
    v = conn.execute("SELECT * FROM versions WHERE id = ?", (vid,)).fetchone()
    if not v:
        raise HTTPException(404, "No such version")
    return v


def eligible(conn, login: str | None, vid: str) -> bool:
    """A player may suggest and vote on a version after reaching a milestone in it or in its parent."""
    if not login:
        return False
    v = conn.execute("SELECT parent FROM versions WHERE id = ?", (vid,)).fetchone()
    ids = [vid] + ([v["parent"]] if v and v["parent"] else [])
    q = f"SELECT 1 FROM milestones WHERE login = ? AND version IN ({','.join('?' * len(ids))}) LIMIT 1"
    return conn.execute(q, (login, *ids)).fetchone() is not None


# A suggestion's votes: the author's level bonus plus every vote's weight (a Curator's vote counts double).
VOTES = "(s.bonus_votes + (SELECT COALESCE(SUM(weight), 0) FROM votes WHERE suggestion = s.id))"


def suggestions_left(conn, login: str) -> int:
    """Ideas per day come from the level (DESIGN.md §10.3). Everyone gets at least 1 per day; the milestone
    check is separate."""
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    used = conn.execute("SELECT COUNT(*) FROM suggestions WHERE login = ? AND created_at >= ?", (login, since)).fetchone()[0]
    return max(0, xp.totals(conn, login)["per_day"] - used)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    login = me(request)
    my_xp = None
    if login:
        with db.connect() as conn:
            my_xp = xp.totals(conn, login)
    return templates.TemplateResponse(request, name, {"me": login, "is_admin": is_admin(login), "my_xp": my_xp, **ctx})


# ---------- pages ----------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.connect() as conn:
        games = conn.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM versions v WHERE v.game = g.slug) AS versions, "
            "(SELECT COUNT(*) FROM suggestions s WHERE s.game = g.slug AND s.status = 'open') AS open_suggestions "
            "FROM games g ORDER BY g.created_at"
        ).fetchall()
    return page(request, "home.html", games=games)


@app.get("/g/{slug}")
def game_main(slug: str):
    with db.connect() as conn:
        g = conn.execute("SELECT main_version FROM games WHERE slug = ?", (slug,)).fetchone()
    if not g or not g["main_version"]:
        raise HTTPException(404, "No such game")
    return RedirectResponse(f"/g/{slug}/v/{g['main_version']}", status_code=302)


@app.get("/g/{slug}/v/{vid}", response_class=HTMLResponse)
def game_version(request: Request, slug: str, vid: str):
    login = me(request)
    with db.connect() as conn:
        game = conn.execute("SELECT * FROM games WHERE slug = ?", (slug,)).fetchone()
        v = version_row(conn, vid)
        if not game or v["game"] != slug:
            raise HTTPException(404, "No such game version")
        mine = {r["milestone"] for r in conn.execute(
            "SELECT milestone FROM milestones WHERE login = ? AND version = ?", (login, vid))} if login else set()
        suggestions = conn.execute(
            f"SELECT s.*, {VOTES} AS votes, "
            "EXISTS(SELECT 1 FROM votes WHERE suggestion = s.id AND login = ?) AS voted "
            "FROM suggestions s WHERE s.game = ? AND s.status = 'open' ORDER BY votes DESC, s.id",
            (login or "", slug),
        ).fetchall()
        builds = conn.execute(
            f"SELECT s.*, {VOTES} AS votes FROM suggestions s "
            "WHERE s.game = ? AND s.status != 'open' ORDER BY COALESCE(s.updated_at, s.created_at) DESC LIMIT 20",
            (slug,),
        ).fetchall()
        versions = conn.execute("SELECT * FROM versions WHERE game = ? ORDER BY created_at DESC", (slug,)).fetchall()
        can = eligible(conn, login, vid)
        left = suggestions_left(conn, login) if login else 0
        per_day = xp.totals(conn, login)["per_day"] if login else 0
    return page(
        request, "game.html", game=game, v=v, milestones=json.loads(v["milestones"]), mine=mine,
        suggestions=suggestions, builds=builds, versions=versions, eligible=can, left=left, per_day=per_day,
        auto_votes=AUTO_BUILD_MIN_VOTES, play_origin=PLAY_URL,
        frame_src=f"{PLAY_URL}/v/{vid}/index.html?t={TICKETS.dumps(vid)}" if login else None,
    )


def version_tree(conn, slug: str, main: str | None) -> list[dict]:
    """The game's versions as a tree (DESIGN.md §8.1): each node has its children, unique players and
    who reached a milestone. A version whose parent is unknown becomes a root. Oldest children first."""
    rows = conn.execute(
        "SELECT v.*, "
        "(SELECT COUNT(DISTINCT login) FROM sessions WHERE version = v.id) AS players, "
        "(SELECT COUNT(DISTINCT login) FROM milestones WHERE version = v.id) AS finishers "
        "FROM versions v WHERE v.game = ? ORDER BY v.created_at, v.id", (slug,)).fetchall()
    nodes = {r["id"]: {**dict(r), "children": [], "on_main_line": False} for r in rows}
    roots = []
    for n in nodes.values():
        (nodes[n["parent"]]["children"] if n["parent"] in nodes else roots).append(n)
    seen, vid = set(), main  # mark the path from the root to the main version
    while vid in nodes and vid not in seen:
        seen.add(vid)
        nodes[vid]["on_main_line"] = True
        vid = nodes[vid]["parent"]
    return roots


@app.get("/g/{slug}/tree", response_class=HTMLResponse)
def game_tree(request: Request, slug: str, here: str = ""):
    with db.connect() as conn:
        game = conn.execute("SELECT * FROM games WHERE slug = ?", (slug,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game")
        roots = version_tree(conn, slug, game["main_version"])
        count = conn.execute("SELECT COUNT(*) FROM versions WHERE game = ?", (slug,)).fetchone()[0]
    return page(request, "tree.html", game=game, roots=roots, count=count, here=here)


# ---------- login ----------

@app.get("/login")
def login(request: Request, next: str = "/"):
    if not CLIENT_ID:
        raise HTTPException(503, "GitHub login is not configured yet")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["next"] = next if next.startswith("/") and not next.startswith("//") else "/"
    query = urlencode({"client_id": CLIENT_ID, "redirect_uri": f"{BASE_URL}/auth/callback", "state": state})
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("oauth_state", None)
    if not code or not expected or not hmac.compare_digest(state, expected):
        return RedirectResponse("/")
    token = httpx.post(
        "https://github.com/login/oauth/access_token",
        data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code},
        headers={"Accept": "application/json"}, timeout=15,
    ).json().get("access_token")
    if not token:
        return RedirectResponse("/")
    user = httpx.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"}, timeout=15).json()
    login = user["login"]
    request.session["login"] = login
    with db.connect() as conn:
        if conn.execute("INSERT OR IGNORE INTO players (login, created_at) VALUES (?, ?)", (login, db.now())).rowcount:
            db.emit(conn, "PlayerJoined", login)
    return RedirectResponse(request.session.pop("next", "/"), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---------- game API (called by static/game.js when the game frame sends a message) ----------

@app.post("/api/sessions")
async def start_session(request: Request):
    login = need_login(request)
    body = await request.json()
    vid, seed = str(body.get("version", "")), body.get("seed")
    with db.connect() as conn:
        version_row(conn, vid)
        first = conn.execute("SELECT 1 FROM sessions WHERE login = ? AND version = ? LIMIT 1", (login, vid)).fetchone() is None
        sid = conn.execute(
            "INSERT INTO sessions (login, version, seed, started_at) VALUES (?, ?, ?, ?)",
            (login, vid, int(seed) if isinstance(seed, int) else None, db.now()),
        ).lastrowid
        db.emit(conn, "SessionStarted", login, version=vid, session=sid)
        if first:
            xp.award_player(conn, vid, login)
    return {"session": sid}


def own_session(conn, sid: int, login: str):
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not s or s["login"] != login:
        raise HTTPException(404, "No such session")
    return s


@app.post("/api/sessions/{sid}/milestone")
async def milestone(request: Request, sid: int):
    login = need_login(request)
    body = await request.json()
    mid = str(body.get("id", ""))
    with db.connect() as conn:
        s = own_session(conn, sid, login)
        v = version_row(conn, s["version"])
        m = next((x for x in json.loads(v["milestones"]) if x["id"] == mid), None)
        if not m:
            raise HTTPException(400, "Unknown milestone")
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(s["started_at"])).total_seconds()
        if elapsed < m["minSeconds"]:
            db.emit(conn, "MilestoneRejected", login, version=s["version"], milestone=mid, seconds=round(elapsed, 1))
            return JSONResponse({"ok": False, "reason": "too fast"}, status_code=400)
        first_here = conn.execute("SELECT 1 FROM milestones WHERE login = ? AND version = ? LIMIT 1",
                                  (login, s["version"])).fetchone() is None
        new = conn.execute(
            "INSERT OR IGNORE INTO milestones (login, version, milestone, session, seconds, reached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (login, s["version"], mid, sid, int(elapsed), db.now()),
        ).rowcount
        if new:
            db.emit(conn, "MilestoneReached", login, game=v["game"], version=s["version"], milestone=mid,
                    seconds=int(elapsed))
            if first_here:
                xp.award(conn, login, xp.MILESTONE, "milestone", game=v["game"], version=s["version"])
        return {"ok": True, "new": bool(new), "name": m["name"], "eligible": eligible(conn, login, s["version"])}


@app.post("/api/sessions/{sid}/end")
async def end_session(request: Request, sid: int):
    login = need_login(request)
    body = await request.json()
    with db.connect() as conn:
        s = own_session(conn, sid, login)
        if s["ended_at"]:
            return {"ok": True}
        secs = int((datetime.now(UTC) - datetime.fromisoformat(s["started_at"])).total_seconds())
        score = body.get("score") if isinstance(body.get("score"), int) else None
        conn.execute("UPDATE sessions SET ended_at = ?, score = ?, seconds = ? WHERE id = ?", (db.now(), score, secs, sid))
        db.emit(conn, "SessionEnded", login, version=s["version"], session=sid, score=score, seconds=secs)
    return {"ok": True}


# ---------- suggestions and votes ----------

@app.post("/g/{slug}/v/{vid}/suggest")
def suggest(request: Request, slug: str, vid: str, title: str = Form(...), body: str = Form("")):
    login = need_login(request)
    title, body = title.strip()[:140], body.strip()[:2000]
    if len(title) < 5:
        raise HTTPException(400, "Describe your idea in at least a few words")
    with db.connect() as conn:
        v = version_row(conn, vid)
        if v["game"] != slug:
            raise HTTPException(404)
        if not eligible(conn, login, vid):
            raise HTTPException(403, "Reach a milestone in this version first")
        if not is_admin(login) and suggestions_left(conn, login) <= 0:
            raise HTTPException(429, "No ideas left today for your level. Vote for others in the meantime!")
        sid = conn.execute(
            "INSERT INTO suggestions (game, base_version, login, title, body, bonus_votes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, vid, login, title, body, xp.totals(conn, login)["bonus_votes"], db.now()),
        ).lastrowid
        db.emit(conn, "SuggestionSubmitted", login, game=slug, version=vid, suggestion=sid, title=title)
    return RedirectResponse(f"/g/{slug}/v/{vid}#suggestions", status_code=303)


@app.post("/suggestions/{sid}/vote")
def vote(request: Request, sid: int):
    login = need_login(request)
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not s or s["status"] != "open":
            raise HTTPException(404, "No such open suggestion")
        if s["login"] == login:
            raise HTTPException(400, "You can't vote for your own idea")
        if not eligible(conn, login, s["base_version"]):
            raise HTTPException(403, "Reach a milestone in this version first")
        weight = xp.totals(conn, login)["vote_weight"]
        if conn.execute("INSERT OR IGNORE INTO votes (suggestion, login, weight, created_at) VALUES (?, ?, ?, ?)",
                        (sid, login, weight, db.now())).rowcount:
            db.emit(conn, "SuggestionVoted", login, game=s["game"], suggestion=sid, weight=weight)
            xp.award(conn, s["login"], xp.VOTE, "vote", game=s["game"], version=s["base_version"])
    return RedirectResponse(f"/g/{s['game']}/v/{s['base_version']}#suggestions", status_code=303)


# ---------- building ideas: admin decides, the worker builds ----------

def set_status(conn, sid: int, status: str, note: str = "", result_version: str | None = None):
    conn.execute("UPDATE suggestions SET status = ?, build_note = ?, result_version = COALESCE(?, result_version), updated_at = ? "
                 "WHERE id = ?", (status, note, result_version, db.now(), sid))


@app.post("/suggestions/{sid}/accept")
def accept(request: Request, sid: int):
    login = need_login(request)
    if not is_admin(login):
        raise HTTPException(403)
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not s or s["status"] not in ("open", "failed"):
            raise HTTPException(400, "Only open or failed ideas can be sent to the builder")
        set_status(conn, sid, "accepted", "Waiting for the builder")
        db.emit(conn, "SuggestionAccepted", login, game=s["game"], suggestion=sid, title=s["title"], author=s["login"])
    return RedirectResponse(f"/g/{s['game']}/v/{s['base_version']}#suggestions", status_code=303)


@app.post("/suggestions/{sid}/reject")
def reject(request: Request, sid: int, reason: str = Form("")):
    login = need_login(request)
    if not is_admin(login):
        raise HTTPException(403)
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404)
        set_status(conn, sid, "rejected", reason.strip()[:300] or "Rejected by the course instructor")
        db.emit(conn, "SuggestionRejected", login, game=s["game"], suggestion=sid, reason=reason.strip()[:300])
    return RedirectResponse(f"/g/{s['game']}/v/{s['base_version']}#suggestions", status_code=303)


def need_worker(request: Request):
    auth = request.headers.get("authorization", "")
    if not WORKER_TOKEN or not hmac.compare_digest(auth, f"Bearer {WORKER_TOKEN}"):
        raise HTTPException(401)


@app.post("/g/{slug}/v/{vid}/promote")
def promote(request: Request, slug: str, vid: str):
    login = need_login(request)
    if not is_admin(login):
        raise HTTPException(403)
    with db.connect() as conn:
        v = version_row(conn, vid)
        if v["game"] != slug:
            raise HTTPException(404)
        conn.execute("UPDATE games SET main_version = ? WHERE slug = ?", (vid, slug))
        db.emit(conn, "VersionPromoted", login, game=slug, version=vid)
    return RedirectResponse(f"/g/{slug}/v/{vid}", status_code=303)


def auto_pick(conn):
    """The top-voted open idea that has enough votes, in a game that has not used today's automatic builds."""
    if AUTO_BUILD_MIN_VOTES <= 0:
        return None
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    s = conn.execute(
        f"SELECT s.*, {VOTES} AS votes FROM suggestions s "
        f"WHERE s.status = 'open' AND {VOTES} >= ? "
        "AND (SELECT COUNT(*) FROM events e WHERE e.type = 'SuggestionAccepted' AND e.actor = 'claude' AND e.at >= ? "
        "     AND json_extract(e.data, '$.game') = s.game) < ? "
        "ORDER BY votes DESC, s.id LIMIT 1",
        (AUTO_BUILD_MIN_VOTES, since, AUTO_BUILDS_PER_DAY),
    ).fetchone()
    if s:
        db.emit(conn, "SuggestionAccepted", "claude", game=s["game"], suggestion=s["id"], title=s["title"], author=s["login"],
                votes=s["votes"], rule=f"top-voted with at least {AUTO_BUILD_MIN_VOTES} votes")
    return s


@app.post("/api/worker/next")
def worker_next(request: Request):
    """Hand the oldest accepted idea to the build worker and mark it as building."""
    need_worker(request)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM suggestions WHERE status = 'accepted' ORDER BY updated_at, id LIMIT 1").fetchone()
        if not s:
            s = auto_pick(conn)
        if not s:
            return {"job": None}
        g = conn.execute("SELECT * FROM games WHERE slug = ?", (s["game"],)).fetchone()
        v = version_row(conn, s["base_version"])
        set_status(conn, s["id"], "building", "The AI builder is working on it")
        db.emit(conn, "BuildStarted", "worker", game=s["game"], suggestion=s["id"], base_version=v["id"])
    return {"job": {"suggestion": s["id"], "game": g["slug"], "game_name": g["name"], "repo": g["repo"],
                    "base_version": v["id"], "base_sha": v["sha"], "title": s["title"], "body": s["body"], "author": s["login"]}}


@app.post("/api/worker/{sid}/progress")
async def worker_progress(request: Request, sid: int):
    """A build step, shown live on the idea's card: {"step": "build", "message": "Writing the code (try 1 of 3)…"}."""
    need_worker(request)
    body = await request.json()
    step, message = str(body.get("step", ""))[:40], str(body.get("message", ""))[:200]
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not s or s["status"] != "building":
            raise HTTPException(400, "This idea is not being built")
        set_status(conn, sid, "building", message)
        db.emit(conn, "BuildProgress", "worker", game=s["game"], suggestion=sid, step=step, message=message)
    return {"ok": True}


@app.get("/api/games/{slug}/builds")
def builds_status(slug: str):
    """Ideas that left the open list: status, live note and the new version. Polled by the game page."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, status, build_note, result_version, COALESCE(updated_at, created_at) AS updated_at FROM suggestions "
            "WHERE game = ? AND status != 'open' ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 20", (slug,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/worker/{sid}/result")
async def worker_result(request: Request, sid: int):
    """status: done (sha + changelog) | rejected (reason shown to the player) | failed (reason)."""
    need_worker(request)
    body = await request.json()
    status, note = body.get("status"), str(body.get("reason", ""))[:500]
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if not s or s["status"] != "building":
            raise HTTPException(400, "This idea is not being built")
        g = conn.execute("SELECT * FROM games WHERE slug = ?", (s["game"],)).fetchone()
        if status == "rejected":
            set_status(conn, sid, "rejected", note)
            db.emit(conn, "SuggestionRejected", "worker", game=s["game"], suggestion=sid, reason=note)
            return {"ok": True}
        if status == "failed":
            set_status(conn, sid, "failed", note)
            db.emit(conn, "BuildFailed", "worker", game=s["game"], suggestion=sid, reason=note)
            return {"ok": True}
        if status != "done":
            raise HTTPException(400, "status must be done, rejected or failed")
    sha, changelog = str(body.get("sha", "")), str(body.get("changelog", ""))[:1000]
    try:
        vid = install(s["game"], g["repo"], sha, parent=s["base_version"], changelog=changelog,
                      credited_login=s["login"], suggestion_id=sid, actor="worker")
    except Exception as e:  # noqa: BLE001 — report any install problem back to the worker
        with db.connect() as conn:
            set_status(conn, sid, "failed", f"Install failed: {e}"[:500])
            db.emit(conn, "BuildFailed", "worker", game=s["game"], suggestion=sid, reason=str(e)[:300])
        raise HTTPException(502, f"install failed: {e}")
    with db.connect() as conn:
        set_status(conn, sid, "live", changelog, result_version=vid)
        db.emit(conn, "BuildSucceeded", "worker", game=s["game"], suggestion=sid, version=vid, author=s["login"])
        xp.award(conn, s["login"], xp.LIVE, "live", game=s["game"], version=vid)
    return {"ok": True, "version": vid}


# ---------- XP: profiles and the admin export ----------

@app.get("/p/{login}", response_class=HTMLResponse)
def profile(request: Request, login: str):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM players WHERE login = ?", (login,)).fetchone():
            raise HTTPException(404, "No such player")
        t = xp.totals(conn, login)
        history = conn.execute("SELECT * FROM xp WHERE login = ? ORDER BY id DESC LIMIT 200", (login,)).fetchall()
        live = conn.execute("SELECT id, game, title, result_version FROM suggestions WHERE login = ? AND status = 'live' "
                            "ORDER BY id DESC", (login,)).fetchall()
    return page(request, "profile.html", player=login, t=t, history=history, live=live, levels=xp.LEVELS,
                unlocks=xp.UNLOCKS)


def roster() -> dict[str, str]:
    """GitHub login → student name, from DATA_DIR/roster.json (built from the EDP roster, as in student-portal)."""
    try:
        return {k.lower(): v for k, v in json.loads((db.DATA_DIR / "roster.json").read_text()).items()}
    except (OSError, ValueError):
        return {}


@app.get("/admin/xp.csv")
def xp_csv(request: Request):
    if not is_admin(me(request)):
        raise HTTPException(403)
    names = roster()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["login", "name", "level", "current_xp", "lifetime_xp", "live_suggestions", "confirmed_bugs"])
    with db.connect() as conn:
        for (login,) in conn.execute("SELECT login FROM players ORDER BY login").fetchall():
            t = xp.totals(conn, login)
            live = conn.execute("SELECT COUNT(*) FROM suggestions WHERE login = ? AND status = 'live'", (login,)).fetchone()[0]
            bugs = conn.execute("SELECT COUNT(*) FROM xp WHERE login = ? AND reason = 'bug'", (login,)).fetchone()[0]
            w.writerow([login, names.get(login.lower(), ""), t["level"], t["current"], t["lifetime"], live, bugs])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="evolvia-xp.csv"'})


# ---------- public event feed (for students' consumers) ----------

@app.get("/api/events")
def events(after: int = 0, limit: int = 200):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, min(limit, 1000))).fetchall()
    return [{"id": r["id"], "type": r["type"], "at": r["at"], "actor": r["actor"], "data": json.loads(r["data"])} for r in rows]
