"""Admin commands (run on the server, e.g. `docker exec <container> evolvia-admin ...`).

  evolvia-admin add-game --game spark-run --name "Spark Run" --repo pbrudny/evolvia-template [--ref main]
  evolvia-admin add-version --game spark-run --ref <branch|sha> [--parent <version>] [--changelog "..."] [--credit <login>] [--main]
  evolvia-admin set-main --game spark-run --version spark-run-abc1234
  evolvia-admin list
"""

import argparse
import json

from . import db
from .versions import install


def main() -> None:
    p = argparse.ArgumentParser(prog="evolvia-admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-game")
    a.add_argument("--game", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--repo", required=True)
    a.add_argument("--ref", default="main")
    v = sub.add_parser("add-version")
    v.add_argument("--game", required=True)
    v.add_argument("--ref", required=True)
    v.add_argument("--parent")
    v.add_argument("--changelog", default="")
    v.add_argument("--credit")
    v.add_argument("--main", action="store_true")
    m = sub.add_parser("set-main")
    m.add_argument("--game", required=True)
    m.add_argument("--version", required=True)
    r = sub.add_parser("set-repo")
    r.add_argument("--game", required=True)
    r.add_argument("--repo", required=True)
    sg = sub.add_parser("add-suggestion", help="for testing the build pipeline")
    sg.add_argument("--game", required=True)
    sg.add_argument("--login", required=True)
    sg.add_argument("--title", required=True)
    sg.add_argument("--body", default="")
    ac = sub.add_parser("accept", help="send an idea to the build worker")
    ac.add_argument("--suggestion", type=int, required=True)
    sub.add_parser("list")
    args = p.parse_args()
    db.init()

    if args.cmd == "add-game":
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO games (slug, name, repo, created_at) VALUES (?, ?, ?, ?)",
                         (args.game, args.name, args.repo, db.now()))
            db.emit(conn, "GameCreated", "admin", game=args.game, name=args.name, repo=args.repo)
        vid = install(args.game, args.repo, args.ref, changelog="Version 0", actor="admin")
        set_main(args.game, vid)
        print(f"game {args.game}: version 0 = {vid}")
    elif args.cmd == "add-version":
        with db.connect() as conn:
            g = conn.execute("SELECT * FROM games WHERE slug = ?", (args.game,)).fetchone()
        if not g:
            raise SystemExit(f"no game {args.game}")
        vid = install(args.game, g["repo"], args.ref, parent=args.parent or g["main_version"],
                      changelog=args.changelog, credited_login=args.credit, actor="admin")
        if args.main:
            set_main(args.game, vid)
        print(f"version {vid}{' (main)' if args.main else ''}")
    elif args.cmd == "set-main":
        set_main(args.game, args.version)
        print(f"{args.game} main = {args.version}")
    elif args.cmd == "set-repo":
        with db.connect() as conn:
            conn.execute("UPDATE games SET repo = ? WHERE slug = ?", (args.repo, args.game))
        print(f"{args.game} repo = {args.repo}")
    elif args.cmd == "accept":
        with db.connect() as conn:
            s = conn.execute("SELECT * FROM suggestions WHERE id = ?", (args.suggestion,)).fetchone()
            if not s or s["status"] not in ("open", "failed"):
                raise SystemExit("only open or failed ideas can be accepted")
            conn.execute("UPDATE suggestions SET status = 'accepted', build_note = 'Waiting for the builder', updated_at = ? WHERE id = ?",
                         (db.now(), args.suggestion))
            db.emit(conn, "SuggestionAccepted", "admin", game=s["game"], suggestion=args.suggestion, title=s["title"], author=s["login"])
        print(f"suggestion {args.suggestion} accepted")
    elif args.cmd == "add-suggestion":
        with db.connect() as conn:
            g = conn.execute("SELECT * FROM games WHERE slug = ?", (args.game,)).fetchone()
            sid = conn.execute("INSERT INTO suggestions (game, base_version, login, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (args.game, g["main_version"], args.login, args.title, args.body, db.now())).lastrowid
            db.emit(conn, "SuggestionSubmitted", "admin", game=args.game, version=g["main_version"], suggestion=sid, title=args.title)
        print(f"suggestion {sid}")
    else:
        with db.connect() as conn:
            for g in conn.execute("SELECT * FROM games"):
                print(json.dumps(dict(g)))
                for v in conn.execute("SELECT id, parent, created_at, credited_login FROM versions WHERE game = ?", (g["slug"],)):
                    print("   ", json.dumps(dict(v)))


def set_main(game: str, vid: str) -> None:
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM versions WHERE id = ? AND game = ?", (vid, game)).fetchone():
            raise SystemExit(f"no version {vid} in {game}")
        conn.execute("UPDATE games SET main_version = ? WHERE slug = ?", (vid, game))
        db.emit(conn, "VersionPromoted", "admin", game=game, version=vid)


if __name__ == "__main__":
    main()
