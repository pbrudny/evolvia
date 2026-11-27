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

from evolvia import db, server, xp  # noqa: E402
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
    assert "You used today's idea (1 per day" in page


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
    assert boss.post(f"/suggestions/{sid}/accept", follow_redirects=False).status_code == 303   # second click: no error

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
        conn.execute("INSERT INTO votes (suggestion, login, created_at) VALUES (?, 'x1', ?)", (low, db.now()))
        conn.execute("INSERT INTO votes (suggestion, login, created_at) VALUES (?, 'x1', ?), (?, 'x2', ?)",
                     (top, db.now(), top, db.now()))
    plain = TestClient(server.app, base_url="http://platform.test")
    job = plain.post("/api/worker/next", headers=W).json()["job"]
    assert job["suggestion"] == top
    assert plain.post("/api/worker/next", headers=W).json() == {"job": None}   # the 1-vote idea waits
    ev = [e for e in plain.get("/api/events").json() if e["type"] == "SuggestionAccepted"][-1]
    assert ev["actor"] == "claude" and ev["data"]["suggestion"] == top
    monkeypatch.setattr(server, "AUTO_BUILDS_PER_DAY", 1)                       # daily limit per game
    with db.connect() as conn:
        conn.execute("INSERT INTO votes (suggestion, login, created_at) VALUES (?, 'x2', ?)", (low, db.now()))
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


# ---------- the Multiverse ----------

def test_multiverse_tree(monkeypatch):
    V2 = "spark-run-ddddddd"
    with db.connect() as conn:
        conn.execute("INSERT INTO versions (id, game, sha, parent, milestones, changelog, credited_login, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (V2, GAME, "d" * 40, V0, json.dumps(MILESTONES),
                                                         "Sparks are green.", "carol", db.now()))
        roots = server.version_tree(conn, GAME, V1)
    assert [r["id"] for r in roots] == [V0]
    kids = {c["id"]: c for c in roots[0]["children"]}
    assert set(kids) >= {V1, V2}
    assert roots[0]["on_main_line"] and kids[V1]["on_main_line"] and not kids[V2]["on_main_line"]
    assert roots[0]["players"] >= 2 and roots[0]["finishers"] >= 2   # alice and dave played V0

    anon = TestClient(server.app, base_url="http://platform.test")
    r = anon.get(f"/g/{GAME}/tree?here={V2}")
    assert r.status_code == 200 and "Multiverse" in r.text
    assert "Sparks are green." in r.text and "@carol" in r.text and "you were here" in r.text
    assert f'href="/g/{GAME}/v/{V2}"' in r.text and "Make main" not in r.text
    assert anon.get("/g/nope/tree").status_code == 404
    boss = client_as("boss", monkeypatch)
    assert "Make main" in boss.get(f"/g/{GAME}/tree").text
    assert f"/g/{GAME}/tree?here={V0}" in boss.get(f"/g/{GAME}/v/{V0}").text


# ---------- XP and levels ----------

def xp_of(login):
    with db.connect() as conn:
        return xp.totals(conn, login)


def test_xp_from_milestone_vote_and_live():
    # alice: milestone on V0 (+5), dave voted for her idea (+1), the idea went live (+30)
    with db.connect() as conn:
        reasons = sorted(r["reason"] for r in conn.execute("SELECT reason FROM xp WHERE login = 'alice'"))
    assert reasons == ["live", "milestone", "vote"]
    t = xp_of("alice")
    assert t["lifetime"] == 36 and t["current"] == 36 and t["level"] == "Contributor" and t["per_day"] == 2
    types = [e["type"] for e in TestClient(server.app, base_url="http://platform.test").get("/api/events").json()]
    assert "XpAwarded" in types and "LevelReached" in types


def test_xp_decays_but_lifetime_does_not():
    month_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    assert round(xp.decayed(100, month_ago)) == 80
    with db.connect() as conn:
        conn.execute("INSERT INTO xp (login, points, reason, at) VALUES ('old', 100, 'manual', ?)", (month_ago,))
        t = xp.totals(conn, "old")
    assert (t["current"], t["lifetime"], t["level"]) == (80, 100, "Contributor")


def test_players_on_your_version_give_xp_up_to_50(monkeypatch):
    with db.connect() as conn:
        conn.execute("UPDATE versions SET credited_login = 'erin' WHERE id = ?", (V1,))
    frank = client_as("frank", monkeypatch)
    frank.post("/api/sessions", json={"version": V1, "seed": 1})
    frank.post("/api/sessions", json={"version": V1, "seed": 2})       # same player again: no XP
    erin = client_as("erin", monkeypatch)
    erin.post("/api/sessions", json={"version": V1, "seed": 1})        # own version: no XP
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM xp WHERE login = 'erin' AND reason = 'players'").fetchone()[0] == 1
        for i in range(60):
            xp.award_player(conn, V1, f"p{i}")
        assert conn.execute("SELECT COUNT(*) FROM xp WHERE login = 'erin' AND reason = 'players'").fetchone()[0] == 50


def test_levels_give_bonus_votes_and_double_votes(monkeypatch):
    with db.connect() as conn:
        xp.award(conn, "gina", 120, "manual")    # Builder
        xp.award(conn, "hank", 700, "manual")    # Curator
        conn.execute("INSERT OR IGNORE INTO players (login, created_at) VALUES ('gina', ?)", (db.now(),))
    for login in ("gina", "hank"):
        c = client_as(login, monkeypatch)
        s = c.post("/api/sessions", json={"version": V0, "seed": 1}).json()["session"]
        age_session(s, 60)
        c.post(f"/api/sessions/{s}/milestone", json={"id": "level3"})
    gina, hank = client_as("gina", monkeypatch), client_as("hank", monkeypatch)
    assert gina.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "lasers everywhere"}, follow_redirects=False).status_code == 303
    assert gina.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "a second idea"}, follow_redirects=False).status_code == 303
    assert gina.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "a third idea"}, follow_redirects=False).status_code == 303
    assert gina.post(f"/g/{GAME}/v/{V0}/suggest", data={"title": "a fourth idea"}, follow_redirects=False).status_code == 429
    with db.connect() as conn:
        sid = conn.execute("SELECT id, bonus_votes FROM suggestions WHERE title = 'lasers everywhere'").fetchone()
    assert sid["bonus_votes"] == 3
    hank.post(f"/suggestions/{sid['id']}/vote", follow_redirects=False)
    with db.connect() as conn:
        votes = conn.execute(f"SELECT {server.VOTES} FROM suggestions s WHERE id = ?", (sid["id"],)).fetchone()[0]
    assert votes == 5   # 3 bonus + a Curator's vote counts 2


def test_profile_and_admin_csv(monkeypatch):
    anon = TestClient(server.app, base_url="http://platform.test")
    r = anon.get("/p/alice")
    assert r.status_code == 200 and "Contributor" in r.text and "Your idea went live" in r.text
    assert anon.get("/p/nobody").status_code == 404
    alice = client_as("alice", monkeypatch)
    assert "Contributor · 36 XP" in alice.get("/").text
    assert alice.get("/admin/xp.csv").status_code == 403
    (db.DATA_DIR / "roster.json").write_text(json.dumps({"Alice": "Alice Example"}))
    csv_text = client_as("boss", monkeypatch).get("/admin/xp.csv").text
    assert "login,name,level,current_xp,lifetime_xp,live_suggestions,confirmed_bugs" in csv_text
    assert "alice,Alice Example,Contributor,36,36,1,0" in csv_text


# ---------- idea rounds and prototype rounds ----------

def fake_install_into_db(game, repo, sha, **kw):
    vid = f"{game}-{sha[:7]}"
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO versions (id, game, sha, parent, milestones, changelog, credited_login, created_at) "
                     "VALUES (?, ?, ?, NULL, ?, ?, ?, ?)", (vid, game, sha, json.dumps(MILESTONES), kw.get("changelog", ""),
                                                            kw.get("credited_login"), db.now()))
    return vid


def test_idea_round_to_new_game(monkeypatch):
    monkeypatch.setattr(server, "install", fake_install_into_db)
    plain = TestClient(server.app, base_url="http://platform.test")
    with db.connect() as conn:   # earlier tests may have left work for the worker
        conn.execute("UPDATE suggestions SET status = 'live' WHERE status IN ('open', 'accepted', 'building')")
    ivy = client_as("ivy", monkeypatch)
    assert ivy.post("/rounds", data={"title": "Round 1"}, follow_redirects=False).status_code == 403   # not an Architect
    boss = client_as("boss", monkeypatch)
    r = boss.post("/rounds", data={"title": "Round 1", "idea_hours": 1, "vote_hours": 1}, follow_redirects=False)
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    assert boss.post("/rounds", data={"title": "Another"}, follow_redirects=False).status_code == 400  # one at a time
    assert "Round 1" in plain.get("/").text

    ideas = {}
    for login, title in [("ivy", "you steal seconds from others"), ("jack", "a snake that grows backwards"),
                         ("kim", "tetris with gravity flipping"), ("leo", "a tiny farm defended by bees")]:
        c = client_as(login, monkeypatch)
        assert c.post(f"/rounds/{rid}/ideas", data={"title": title}, follow_redirects=False).status_code == 303
        with db.connect() as conn:
            ideas[login] = conn.execute("SELECT id FROM ideas WHERE login = ? AND round = ?", (login, rid)).fetchone()["id"]
    assert ivy.post(f"/rounds/{rid}/ideas", data={"title": "a second idea"}, follow_redirects=False).status_code == 429
    assert ivy.post(f"/ideas/{ideas['ivy']}/vote", follow_redirects=False).status_code == 400          # own idea
    for voter, idea in [("jack", "ivy"), ("kim", "ivy"), ("ivy", "jack"), ("leo", "kim")]:
        client_as(voter, monkeypatch).post(f"/ideas/{ideas[idea]}/vote", follow_redirects=False)

    boss.post(f"/rounds/{rid}/next", follow_redirects=False)          # end the idea phase now
    with db.connect() as conn:
        queued = [r["id"] for r in conn.execute("SELECT id FROM ideas WHERE status = 'queued' ORDER BY id")]
    assert queued == [ideas["ivy"], ideas["jack"], ideas["kim"]]   # top 3; leo's idea stays on the board

    jobs = [plain.post("/api/worker/next", headers=W).json()["job"] for _ in range(3)]
    assert [j["kind"] for j in jobs] == ["prototype"] * 3 and jobs[0]["repo"] == "pbrudny/evolvia-prototypes"
    assert plain.post(jobs[0]["progress"], headers=W, json={"message": "Writing the code…"}).json() == {"ok": True}
    plain.post(jobs[0]["result"], headers=W, json={"status": "done", "sha": "1" * 40, "changelog": "Steal time!", "name": "Time Thief"})
    plain.post(jobs[1]["result"], headers=W, json={"status": "done", "sha": "2" * 40, "changelog": "Backwards.", "name": "Snake Back"})
    plain.post(jobs[2]["result"], headers=W, json={"status": "failed", "reason": "gates kept failing"})
    with db.connect() as conn:
        assert server.round_row(conn, rid)["status"] == "voting"
    page = plain.get(f"/rounds/{rid}").text
    assert "Time Thief" in page and "Snake Back" in page and f"/g/proto-{ideas['ivy']}/v/" in page

    # voting needs a milestone in that prototype
    proto_v = f"proto-{ideas['ivy']}-1111111"
    assert "Prototype" in ivy.get(f"/g/proto-{ideas['ivy']}/v/{proto_v}").text
    assert ivy.post(f"/g/proto-{ideas['ivy']}/v/{proto_v}/suggest", data={"title": "more time"}).status_code == 400
    assert jack_votes(monkeypatch, rid, ideas["ivy"]) == 403
    s = client_as("jack", monkeypatch).post("/api/sessions", json={"version": proto_v, "seed": 1}).json()["session"]
    age_session(s, 60)
    client_as("jack", monkeypatch).post(f"/api/sessions/{s}/milestone", json={"id": "level3"})
    assert jack_votes(monkeypatch, rid, ideas["ivy"]) == 303

    boss.post(f"/rounds/{rid}/next", follow_redirects=False)          # end voting now
    with db.connect() as conn:
        r = server.round_row(conn, rid)
        assert (r["status"], r["winner"], r["game"]) == ("creating", ideas["ivy"], "time-thief")
        st = {row["login"]: row["status"] for row in conn.execute("SELECT login, status FROM ideas WHERE round = ?", (rid,))}
    assert st == {"ivy": "won", "jack": "open", "kim": "open", "leo": "open"}   # the others stay on the board
    assert any(h["reason"] == "prototype" and h["points"] == 50 for h in xp_history("ivy"))

    job = plain.post("/api/worker/next", headers=W).json()["job"]
    assert job["kind"] == "create_game" and job["slug"] == "time-thief" and job["sha"] == "1" * 40
    assert plain.post("/api/worker/next", headers=W).json()["job"] is None   # handed out once
    r = plain.post(job["result"], headers=W, json={"status": "done", "repo": "pbrudny/evolvia-time-thief", "sha": "1" * 40})
    assert r.json()["ok"]
    with db.connect() as conn:
        g = conn.execute("SELECT * FROM games WHERE slug = 'time-thief'").fetchone()
        assert g["kind"] == "game" and g["repo"] == "pbrudny/evolvia-time-thief" and g["main_version"]
        assert server.round_row(conn, rid)["status"] == "closed"
    home = plain.get("/").text
    assert "Time Thief" in home and "proto-" not in home
    types = [e["type"] for e in plain.get("/api/events").json()]
    for t in ("IdeaRoundOpened", "IdeaSubmitted", "IdeaVoted", "IdeaRoundClosed", "PrototypeRequested", "PrototypeBuilt",
              "PrototypeVoted", "GameCreated"):
        assert t in types, t


def jack_votes(monkeypatch, rid, idea):
    return client_as("jack", monkeypatch).post(f"/rounds/{rid}/vote", data={"idea": idea}, follow_redirects=False).status_code


def xp_history(login):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM xp WHERE login = ?", (login,))]


# ---------- merges ----------

def test_merge_versions(monkeypatch):
    with db.connect() as conn:   # nothing else waiting for the worker
        conn.execute("UPDATE suggestions SET status = 'live' WHERE status IN ('open', 'accepted', 'building')")
        conn.execute("UPDATE ideas SET status = 'open' WHERE status IN ('queued', 'building')")
        conn.execute("UPDATE rounds SET status = 'closed'")
    plain = TestClient(server.app, base_url="http://platform.test")
    dave = client_as("dave", monkeypatch)
    assert dave.post(f"/g/{GAME}/merge", data={"versions": [V0, V1]}, follow_redirects=False).status_code == 403
    boss = client_as("boss", monkeypatch)
    assert "Merge selected" in boss.get(f"/g/{GAME}/tree").text and "Merge selected" not in dave.get(f"/g/{GAME}/tree").text
    assert boss.post(f"/g/{GAME}/merge", data={"versions": [V1]}, follow_redirects=False).status_code == 400   # one only
    assert boss.post(f"/g/{GAME}/merge", data={"versions": [V1, "nope"]}, follow_redirects=False).status_code == 404
    assert boss.post(f"/g/{GAME}/merge", data={"versions": [V1, V0]}, follow_redirects=False).status_code == 303

    job = plain.post("/api/worker/next", headers=W).json()["job"]
    assert job["kind"] == "merge" and [v["id"] for v in job["versions"]] == [V1, V0] and job["repo"] == "pbrudny/evolvia-template"
    assert plain.post("/api/worker/next", headers=W).json()["job"] is None
    assert plain.post(job["progress"], headers=W, json={"message": "Merging…"}).json() == {"ok": True}
    monkeypatch.setattr(server, "install", fake_install_into_db)
    r = plain.post(job["result"], headers=W, json={"status": "done", "sha": "e" * 40, "changelog": "Combines A; B."})
    vid = r.json()["version"]
    with db.connect() as conn:
        v = conn.execute("SELECT * FROM versions WHERE id = ?", (vid,)).fetchone()
        m = conn.execute("SELECT * FROM merges WHERE id = ?", (job["merge"],)).fetchone()
    assert json.loads(v["merged_from"]) == [V1, V0] and (m["status"], m["result_version"]) == ("live", vid)
    page = boss.get(f"/g/{GAME}/tree").text
    assert "Merged from" in page and "Play the merged version" in page
    assert "VersionsMerged" in [e["type"] for e in plain.get("/api/events").json()]
