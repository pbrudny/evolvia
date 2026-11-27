"""Evolvia platform: GitHub login, games in a sandboxed frame, milestones, "What if…?" suggestions and votes.

One app, two addresses:
- BASE_URL (evolvia.atabuilderslab.pl): pages, login, API. Has the session cookie.
- PLAY_URL (evolvia-play.atabuilderslab.pl): only the static files of game versions. No cookies, strict CSP,
  shown inside a sandboxed iframe, so AI-written game code can never reach the platform or the player's session.
"""

import hmac
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
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .versions import version_dir

CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
PLAY_URL = os.environ.get("PLAY_URL", "http://localhost:8000").rstrip("/")
PLAY_HOST = urlsplit(PLAY_URL).netloc
ADMINS = {a.strip().lower() for a in os.environ.get("ADMIN_LOGINS", "").split(",") if a.strip()}
SUGGESTIONS_PER_DAY = int(os.environ.get("SUGGESTIONS_PER_DAY", "1"))

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
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
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


def suggestions_left(conn, login: str) -> int:
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    used = conn.execute("SELECT COUNT(*) FROM suggestions WHERE login = ? AND created_at >= ?", (login, since)).fetchone()[0]
    return max(0, SUGGESTIONS_PER_DAY - used)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"me": me(request), "is_admin": is_admin(me(request)), **ctx})


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
            "SELECT s.*, (SELECT COUNT(*) FROM votes WHERE suggestion = s.id) AS votes, "
            "EXISTS(SELECT 1 FROM votes WHERE suggestion = s.id AND login = ?) AS voted "
            "FROM suggestions s WHERE s.game = ? AND s.status = 'open' ORDER BY votes DESC, s.id",
            (login or "", slug),
        ).fetchall()
        versions = conn.execute("SELECT * FROM versions WHERE game = ? ORDER BY created_at DESC", (slug,)).fetchall()
        can = eligible(conn, login, vid)
        left = suggestions_left(conn, login) if login else 0
    return page(
        request, "game.html", game=game, v=v, milestones=json.loads(v["milestones"]), mine=mine,
        suggestions=suggestions, versions=versions, eligible=can, left=left,
        frame_src=f"{PLAY_URL}/v/{vid}/index.html", play_origin=PLAY_URL,
    )


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
    login = me(request)
    body = await request.json()
    vid, seed = str(body.get("version", "")), body.get("seed")
    if not login:
        return {"session": None, "note": "Not signed in: playing works, milestones are not recorded."}
    with db.connect() as conn:
        version_row(conn, vid)
        sid = conn.execute(
            "INSERT INTO sessions (login, version, seed, started_at) VALUES (?, ?, ?, ?)",
            (login, vid, int(seed) if isinstance(seed, int) else None, db.now()),
        ).lastrowid
        db.emit(conn, "SessionStarted", login, version=vid, session=sid)
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
        new = conn.execute(
            "INSERT OR IGNORE INTO milestones (login, version, milestone, session, seconds, reached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (login, s["version"], mid, sid, int(elapsed), db.now()),
        ).rowcount
        if new:
            db.emit(conn, "MilestoneReached", login, game=v["game"], version=s["version"], milestone=mid,
                    seconds=int(elapsed))
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
            raise HTTPException(429, "One suggestion per day. Vote for others in the meantime!")
        sid = conn.execute(
            "INSERT INTO suggestions (game, base_version, login, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (slug, vid, login, title, body, db.now()),
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
        if conn.execute("INSERT OR IGNORE INTO votes (suggestion, login, created_at) VALUES (?, ?, ?)",
                        (sid, login, db.now())).rowcount:
            db.emit(conn, "SuggestionVoted", login, game=s["game"], suggestion=sid)
    return RedirectResponse(f"/g/{s['game']}/v/{s['base_version']}#suggestions", status_code=303)


# ---------- public event feed (for students' consumers) ----------

@app.get("/api/events")
def events(after: int = 0, limit: int = 200):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, min(limit, 1000))).fetchall()
    return [{"id": r["id"], "type": r["type"], "at": r["at"], "actor": r["actor"], "data": json.loads(r["data"])} for r in rows]
