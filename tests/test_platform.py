"""Platform tests: two hosts, login, milestones with the time check, suggestions, votes, events. No network."""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="evolvia-test-")
os.environ["BASE_URL"] = "http://platform.test"
os.environ["PLAY_URL"] = "http://play.test"
os.environ["GITHUB_CLIENT_ID"] = "test-client"
os.environ["ADMIN_LOGINS"] = "boss"
os.environ["WORKER_TOKEN"] = "worker-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from evolvia import db, server  # noqa: E402
from evolvia.versions import version_dir  # noqa: E402

GAME, V0, V1 = "spark-run", "spark-run-aaaaaaa", "spark-run-bbbbbbb"
MILESTONES = [{"id": "level3", "name": "Reach level 3", "minSeconds": 6.0}]


@pytest.fixture(scope="module", autouse=True)
def seed_game():
    with db.connect() as conn:
        conn.execute("INSERT INTO games (slug, name, repo, main_version, created_at) VALUES (?, ?, ?, ?, ?)",
                     (GAME, "Spark Run", "pbrudny/evolvia-template", V0, db.now()))
        for vid, parent in ((V0, None), (V1, V0)):
            conn.execute("INSERT INTO versions (id, game, sha, parent, milestones, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (vid, GAME, vid[-7:] * 6, parent, json.dumps(MILESTONES), db.now()))
            d = version_dir(vid)
            (d / "game").mkdir(parents=True, exist_ok=True)
            (d / "index.html").write_text("<!doctype html><title>game</title>")
            (d / "game" / "main.js").write_text("console.log('hi')")


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def client_as(login: str, monkeypatch) -> TestClient:
    c = TestClient(server.app, base_url="http://platform.test")
    monkeypatch.setattr(server.httpx, "post", lambda *a, **k: FakeResp({"access_token": "t"}))
    monkeypatch.setattr(server.httpx, "get", lambda *a, **k: FakeResp({"login": login}))
    r = c.get("/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    c.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
    return c


def age_session(sid: int, seconds: int):
    with db.connect() as conn:
        t = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (t, sid))


# ---------- the play host ----------

def test_play_host_serves_only_game_files_without_cookies():
    play = TestClient(server.app, base_url="http://play.test")
    assert play.get(f"/v/{V0}/index.html").status_code == 403                      # no ticket
    assert play.get(f"/v/{V0}/index.html?t=forged").status_code == 403
    assert play.get(f"/v/{V0}/index.html?t={server.TICKETS.dumps(V1)}").status_code == 403  # ticket for another version
    assert play.get(f"/v/{V0}/index.html?t={server.TICKETS.dumps(V0)}").status_code == 200
    r = play.get(f"/v/{V0}/game/main.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "connect-src 'none'" in r.headers["content-security-policy"]
    assert "frame-ancestors http://platform.test" in r.headers["content-security-policy"]
    assert r.headers["access-control-allow-origin"] == "*"
    assert "set-cookie" not in r.headers
    assert play.get("/").status_code == 404                      # no platform pages on the play host
    assert play.get("/api/events").status_code == 404
    assert play.get(f"/v/{V0}/../../evolvia.db").status_code == 404  # no path traversal
    assert play.post(f"/v/{V0}/index.html").status_code == 404


def test_platform_page_frames_the_play_host(monkeypatch):
    anon = TestClient(server.app, base_url="http://platform.test")
    r = anon.get(f"/g/{GAME}/v/{V0}")
    assert r.status_code == 200 and "Sign in to play" in r.text and "<iframe" not in r.text
    c = client_as("erin", monkeypatch)
    r = c.get(f"/g/{GAME}/v/{V0}")
    assert 'sandbox="allow-scripts"' in r.text and f"http://play.test/v/{V0}/index.html?t=" in r.text
    assert "frame-src http://play.test" in r.headers["content-security-policy"]
    assert c.get(f"/g/{GAME}", follow_redirects=False).headers["location"] == f"/g/{GAME}/v/{V0}"


# ---------- sessions and milestones ----------

def test_sessions_need_sign_in():
    c = TestClient(server.app, base_url="http://platform.test")
    assert c.post("/api/sessions", json={"version": V0, "seed": 1}).status_code == 401


def test_milestone_time_check_and_unlock(monkeypatch):
    c = client_as("alice", monkeypatch)
    sid = c.post("/api/sessions", json={"version": V0, "seed": 5}).json()["session"]
    r = c.post(f"/api/sessions/{sid}/milestone", json={"id": "level3"})
    assert r.status_code == 400 and r.json()["reason"] == "too fast"
    age_session(sid, 30)
    r = c.post(f"/api/sessions/{sid}/milestone", json={"id": "level3"})
    assert r.json() == {"ok": True, "new": True, "name": "Reach level 3", "eligible": True}
    assert c.post(f"/api/sessions/{sid}/milestone", json={"id": "nope"}).status_code == 400
    # someone else can't report on alice's session
    bob = client_as("bob", monkeypatch)
    assert bob.post(f"/api/sessions/{sid}/milestone", json={"id": "level3"}).status_code == 404


# ---------- suggestions and votes ----------

def test_suggest_needs_a_milestone_and_is_limited(monkeypatch):
    carol = client_as("carol", monkeypatch)
    r = carol.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "the drifters were slower"}, follow_redirects=False)
    assert r.status_code == 403
    alice = client_as("alice", monkeypatch)   # alice reached level3 on V0 above
    r = alice.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "sparks moved slowly"}, follow_redirects=False)
    assert r.status_code == 303
    r = alice.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "a second idea today"}, follow_redirects=False)
    assert r.status_code == 429
    # a milestone in the parent version also unlocks the child version
    page = alice.get(f"/g/{GAME}/v/{V1}").text
    assert "You used today's suggestion" in page


def test_votes(monkeypatch):
    with db.connect() as conn:
        sid = conn.execute("SELECT id FROM suggestions WHERE login = 'alice'").fetchone()["id"]
    alice = client_as("alice", monkeypatch)
    assert alice.post(f"/suggestions/{sid}/vote", follow_redirects=False).status_code == 400  # own idea
    dave = client_as("dave", monkeypatch)
    assert dave.post(f"/suggestions/{sid}/vote", follow_redirects=False).status_code == 403   # hasn't played
    s = dave.post("/api/sessions", json={"version": V0, "seed": 1}).json()["session"]
    age_session(s, 60)
    dave.post(f"/api/sessions/{s}/milestone", json={"id": "level3"})
    assert dave.post(f"/suggestions/{sid}/vote", follow_redirects=False).status_code == 303
    assert dave.post(f"/suggestions/{sid}/vote", follow_redirects=False).status_code == 303   # second vote ignored
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM votes WHERE suggestion = ?", (sid,)).fetchone()[0] == 1


def test_event_feed():
    c = TestClient(server.app, base_url="http://platform.test")
    types = [e["type"] for e in c.get("/api/events").json()]
    for t in ("PlayerJoined", "SessionStarted", "MilestoneRejected", "MilestoneReached", "SuggestionSubmitted", "SuggestionVoted"):
        assert t in types, t
    last = c.get("/api/events").json()[-1]["id"]
    assert c.get(f"/api/events?after={last}").json() == []


# ---------- build pipeline: admin accepts, worker builds ----------

W = {"Authorization": "Bearer worker-secret"}


def test_build_flow(monkeypatch):
    with db.connect() as conn:
        sid = conn.execute("SELECT id FROM suggestions WHERE login = 'alice'").fetchone()["id"]
    plain = TestClient(server.app, base_url="http://platform.test")
    assert plain.post("/api/worker/next").status_code == 401                       # no token
    assert plain.post("/api/worker/next", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert plain.post("/api/worker/next", headers=W).json() == {"job": None}       # nothing accepted yet

    dave = client_as("dave", monkeypatch)
    assert dave.post(f"/suggestions/{sid}/accept", follow_redirects=False).status_code == 403   # not an admin
    boss = client_as("boss", monkeypatch)
    assert boss.post(f"/suggestions/{sid}/accept", follow_redirects=False).status_code == 303

    job = plain.post("/api/worker/next", headers=W).json()["job"]
    assert job["suggestion"] == sid and job["repo"] == "pbrudny/evolvia-template" and job["author"] == "alice"
    assert job["base_version"] == V0 and len(job["base_sha"]) == 42
    assert plain.post("/api/worker/next", headers=W).json() == {"job": None}       # handed out once

    installed = {}

    def fake_install(game, repo, sha, **kw):
        installed.update(game=game, repo=repo, sha=sha, **kw)
        return f"{game}-{sha[:7]}"

    monkeypatch.setattr(server, "install", fake_install)
    r = plain.post(f"/api/worker/{sid}/result", headers=W, json={"status": "done", "sha": "c" * 40, "changelog": "Sparks drift slowly."})
    assert r.json() == {"ok": True, "version": "spark-run-ccccccc"}
    assert installed["parent"] == V0 and installed["credited_login"] == "alice" and installed["suggestion_id"] == sid
    with db.connect() as conn:
        row = conn.execute("SELECT status, result_version FROM suggestions WHERE id = ?", (sid,)).fetchone()
    assert (row["status"], row["result_version"]) == ("live", "spark-run-ccccccc")
    assert plain.post(f"/api/worker/{sid}/result", headers=W, json={"status": "done", "sha": "c" * 40}).status_code == 400

    types = [e["type"] for e in plain.get("/api/events").json()]
    for t in ("SuggestionAccepted", "BuildStarted", "BuildSucceeded"):
        assert t in types, t


def test_worker_can_reject_with_a_reason(monkeypatch):
    with db.connect() as conn:
        sid = conn.execute("INSERT INTO suggestions (game, base_version, login, title, created_at) VALUES (?, ?, ?, ?, ?)",
                           (GAME, V0, "carol", "the game showed ads", db.now())).lastrowid
    boss = client_as("boss", monkeypatch)
    boss.post(f"/suggestions/{sid}/accept", follow_redirects=False)
    plain = TestClient(server.app, base_url="http://platform.test")
    assert plain.post("/api/worker/next", headers=W).json()["job"]["suggestion"] == sid
    plain.post(f"/api/worker/{sid}/result", headers=W, json={"status": "rejected", "reason": "Ads break rule 5."})
    page = boss.get(f"/g/{GAME}/v/{V0}").text
    assert "Ads break rule 5." in page and "status-rejected" in page


def test_claude_picks_the_top_voted_idea(monkeypatch):
    monkeypatch.setattr(server, "AUTO_BUILD_MIN_VOTES", 2)
    with db.connect() as conn:
        conn.execute("UPDATE suggestions SET status = 'live' WHERE status IN ('open', 'accepted', 'building')")
        low = conn.execute("INSERT INTO suggestions (game, base_version, login, title, created_at) VALUES (?, ?, ?, ?, ?)",
                           (GAME, V0, "carol", "one vote only", db.now())).lastrowid
        top = conn.execute("INSERT INTO suggestions (game, base_version, login, title, created_at) VALUES (?, ?, ?, ?, ?)",
                           (GAME, V0, "carol", "two votes", db.now())).lastrowid
        conn.execute("INSERT INTO votes VALUES (?, 'x1', ?)", (low, db.now()))
        conn.execute("INSERT INTO votes VALUES (?, 'x1', ?), (?, 'x2', ?)", (top, db.now(), top, db.now()))
    plain = TestClient(server.app, base_url="http://platform.test")
    job = plain.post("/api/worker/next", headers=W).json()["job"]
    assert job["suggestion"] == top
    assert plain.post("/api/worker/next", headers=W).json() == {"job": None}   # the 1-vote idea waits
    ev = [e for e in plain.get("/api/events").json() if e["type"] == "SuggestionAccepted"][-1]
    assert ev["actor"] == "claude" and ev["data"]["suggestion"] == top
    monkeypatch.setattr(server, "AUTO_BUILDS_PER_DAY", 1)                       # daily limit per game
    with db.connect() as conn:
        conn.execute("INSERT INTO votes VALUES (?, 'x2', ?)", (low, db.now()))
    assert plain.post("/api/worker/next", headers=W).json() == {"job": None}


def test_build_progress_is_shown_live(monkeypatch):
    with db.connect() as conn:
        conn.execute("UPDATE suggestions SET status = 'live' WHERE status IN ('open', 'accepted', 'building')")
        sid = conn.execute("INSERT INTO suggestions (game, base_version, login, title, created_at) VALUES (?, ?, ?, ?, ?)",
                           (GAME, V0, "carol", "spiders instead of balls", db.now())).lastrowid
    boss = client_as("boss", monkeypatch)
    boss.post(f"/suggestions/{sid}/accept", follow_redirects=False)
    plain = TestClient(server.app, base_url="http://platform.test")
    r = plain.post(f"/api/worker/{sid}/progress", headers=W, json={"step": "x", "message": "y"})
    assert r.status_code == 400  # not building yet
    plain.post("/api/worker/next", headers=W)
    assert plain.post(f"/api/worker/{sid}/progress", json={"step": "build", "message": "hi"}).status_code == 401       # no token
    r = plain.post(f"/api/worker/{sid}/progress", headers=W, json={"step": "build", "message": "Writing the code…"})
    assert r.json() == {"ok": True}
    row = next(b for b in plain.get(f"/api/games/{GAME}/builds").json() if b["id"] == sid)
    assert row["status"] == "building" and row["build_note"] == "Writing the code…"
    assert any(e["type"] == "BuildProgress" and e["data"]["message"] == "Writing the code…" for e in plain.get("/api/events").json())
    page = boss.get(f"/g/{GAME}/v/{V0}").text
    assert f'data-build="{sid}"' in page and "/static/builds.js" in page
