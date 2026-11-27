"""Game versions: download a commit of a game repo and keep its playable files under DATA_DIR/versions."""

import io
import json
import re
import shutil
import tarfile
from pathlib import PurePosixPath

import httpx

from .db import DATA_DIR, connect, emit, now

VERSIONS = DATA_DIR / "versions"
# Only these files are served to players: the page, the game code and the milestones.
ALLOWED = re.compile(r"^(index\.html|milestones\.json|game/[A-Za-z0-9_\-./]+\.(js|json|png|webp|svg|css))$")


def version_dir(version_id: str):
    return VERSIONS / version_id


def resolve_sha(repo: str, ref: str) -> str:
    r = httpx.get(
        f"https://api.github.com/repos/{repo}/commits/{ref}",
        headers={"Accept": "application/vnd.github.sha"},
        timeout=20,
    )
    r.raise_for_status()
    return r.text.strip()


def _check_milestones(data) -> list[dict]:
    if not isinstance(data, list) or not data:
        raise ValueError("milestones.json must be a non-empty list")
    for m in data:
        if not (isinstance(m, dict) and m.get("id") and m.get("name") and float(m.get("minSeconds", 0)) > 0):
            raise ValueError(f"bad milestone: {m!r}")
    return [{"id": str(m["id"]), "name": str(m["name"]), "minSeconds": float(m["minSeconds"])} for m in data]


def install(game: str, repo: str, ref: str, *, parent: str | None = None, changelog: str = "",
            credited_login: str | None = None, suggestion_id: int | None = None, actor: str = "system") -> str:
    """Download repo@ref, keep the allowed files, register the version. Returns the version id."""
    sha = resolve_sha(repo, ref)
    vid = f"{game}-{sha[:7]}"
    r = httpx.get(f"https://codeload.github.com/{repo}/tar.gz/{sha}", timeout=60, follow_redirects=True)
    r.raise_for_status()

    target = version_dir(vid)
    tmp = target.with_name(vid + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    kept = 0
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = "/".join(PurePosixPath(member.name).parts[1:])  # drop the "<repo>-<sha>/" prefix
            if not ALLOWED.match(rel) or ".." in rel:
                continue
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tar.extractfile(member).read())
            kept += 1
    if not (tmp / "index.html").exists() or not (tmp / "milestones.json").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("the commit has no index.html or milestones.json")
    milestones = _check_milestones(json.loads((tmp / "milestones.json").read_text()))
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)

    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO versions (id, game, sha, parent, milestones, changelog, credited_login, "
            "suggestion_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, game, sha, parent, json.dumps(milestones), changelog, credited_login, suggestion_id, now()),
        )
        emit(conn, "VersionDeployed", actor, game=game, version=vid, parent=parent, files=kept,
             changelog=changelog, credited=credited_login)
    return vid
