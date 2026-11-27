"""Build worker: turns an accepted player idea into a new game version.

Runs on a machine with the `claude` CLI (Claude subscription), git, gh and Docker:

    evolvia-worker run [--once]          poll the platform for accepted ideas and build them
    evolvia-worker try --repo pbrudny/evolvia-spark-run --title "..." [--author pbrudny] [--push]
                                         build an idea locally without the platform (for testing)

Jobs (field "kind"): "suggestion" (a "What if…?" idea for a game), "prototype" (a game idea from an idea round,
built from evolvia-template on a branch of the prototypes repo) and "create_game" (the round winner gets its own repo).

Per idea: classifier (on topic? safe?) -> builder (edits files only, no shell) -> gates in a Docker sandbox with no
network (up to 3 rounds) -> reviewer (up to 2 rounds) -> commit with Co-authored-by: <player> -> push -> report.
AI-written code is never executed on this machine: only inside `docker run --network none`.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

AGENTS = Path(__file__).parent / "agents"
WORK = Path(os.environ.get("EVOLVIA_WORK", Path.home() / ".evolvia-worker"))
PLATFORM = os.environ.get("EVOLVIA_URL", "https://evolvia.atabuilderslab.pl").rstrip("/")
TOKEN = os.environ.get("EVOLVIA_WORKER_TOKEN", "")
IMAGE = os.environ.get("EVOLVIA_SANDBOX_IMAGE", "node:20.20-alpine")
MAX_BUILD_ROUNDS, MAX_REVIEW_ROUNDS = 3, 2
# The builder may only change game files. Anything else it touches is reverted.
PROTECTED = re.compile(r"^(game/platform\.js|gates\.mjs|constitution\.md|package\.json|\.github/.*|\.claude/.*|README\.md|LICENSE.*)$")
ALLOWED = re.compile(r"^(game/[A-Za-z0-9_\-./]+|tests/[A-Za-z0-9_\-./]+\.mjs|bot\.js|index\.html|milestones\.json)$")

SCHEMAS = {
    "classifier": {"type": "object", "required": ["accept", "reason", "task"], "properties": {
        "accept": {"type": "boolean"},
        "reason": {"type": "string", "description": "shown to the player when rejected; short and friendly"},
        "task": {"type": "string", "description": "the task for the builder, when accepted"},
        "name": {"type": "string", "description": "prototypes only: a short catchy name for the game, 1-3 words"}}},
    "builder": {"type": "object", "required": ["status", "summary", "changelog", "files_changed"], "properties": {
        "status": {"enum": ["done", "blocked"]},
        "summary": {"type": "string"}, "changelog": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "blocked": {"type": ["string", "null"]}}},
    "reviewer": {"type": "object", "required": ["approve", "blocking", "nits"], "properties": {
        "approve": {"type": "boolean"},
        "blocking": {"type": "array", "items": {"type": "string"}},
        "nits": {"type": "array", "items": {"type": "string"}}}},
}
TOOLS = {
    "classifier": "",
    "builder": "Read,Edit,Write,Grep,Glob",
    "reviewer": "Read,Grep,Glob,Bash",
}
ALLOW = {"reviewer": ["Bash(git diff:*)", "Bash(git show:*)", "Bash(git log:*)", "Bash(git status:*)"]}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, timeout: int = 600,
        env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(p.stderr or p.stdout)[-800:]}")
    return p


# ---------- agents ----------

def claude_env() -> dict:
    """The subscription login is stored for an explicit CLAUDE_CONFIG_DIR. launchd starts the worker without it, and then
    `claude -p` answers "Not logged in", so always set it (EVOLVIA_CLAUDE_CONFIG_DIR overrides, default ~/.claude)."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = (os.environ.get("EVOLVIA_CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
                                or str(Path.home() / ".claude"))
    env.setdefault("USER", os.environ.get("LOGNAME") or Path.home().name)
    # Builds run on Piotr's Claude subscription only: never let an API key switch claude -p to API billing.
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(key, None)
    return env


def subscription_ok() -> bool:
    """True when claude -p is signed in with a claude.ai subscription (not an API key or console account)."""
    try:
        status = json.loads(run(["claude", "auth", "status"], check=False, timeout=60, env=claude_env()).stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
        return False
    return bool(status.get("loggedIn")) and status.get("authMethod") == "claude.ai"


def schema_errors(schema: dict, v, path: str = "") -> list[str]:
    if "enum" in schema:
        return [] if v in schema["enum"] else [f"{path or 'value'} must be one of {schema['enum']}"]
    t = schema.get("type")
    if t == "object":
        if not isinstance(v, dict):
            return [f"{path or 'answer'} must be an object"]
        errs = [f"missing key {k}" for k in schema.get("required", []) if k not in v]
        for k, sub in schema.get("properties", {}).items():
            if k in v and v[k] is not None:
                errs += schema_errors(sub, v[k], k)
        return errs
    if t == "array":
        return [] if isinstance(v, list) else [f"{path} must be an array"]
    return []


def parse_result(out: str) -> tuple[dict | None, dict]:
    res = json.loads(out)
    if isinstance(res, list):
        res = next((m for m in reversed(res) if m.get("type") == "result"), {})
    if res.get("is_error"):
        raise RuntimeError(f"claude error: {str(res.get('result'))[-400:]}")
    data = res.get("structured_output")
    if not data:
        m = re.search(r"\{[\s\S]*\}", str(res.get("result", "")))
        try:
            data = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            data = None
    return data, res


def agent(name: str, prompt: str, cwd: Path) -> dict:
    """Run one agent with `claude -p`, isolated from the user's settings, hooks, MCP servers and CLAUDE.md."""
    schema = SCHEMAS[name]
    full = f"{prompt}\n\n---\nEnd your answer with one JSON object (no code fence) matching this JSON Schema:\n{json.dumps(schema)}"
    flags = ["--output-format", "json", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--append-system-prompt", (AGENTS / f"{name}.md").read_text(), "--tools", TOOLS[name]]
    if name == "builder":
        flags += ["--permission-mode", "acceptEdits"]
    if ALLOW.get(name):
        flags += ["--allowedTools", *ALLOW[name]]
    # The prompt goes right after -p: --tools and --allowedTools take lists and would swallow a later prompt.
    call = lambda text: run(["claude", "-p", text, *flags], cwd=cwd, check=False, timeout=1800, env=claude_env())  # noqa: E731
    t0 = time.time()
    p = call(full)
    if not p.stdout.strip():
        raise RuntimeError(f"{name}: claude printed nothing: {p.stderr[-600:]}")
    data, res = parse_result(p.stdout)
    errs = schema_errors(schema, data) if data else ["no JSON"]
    if errs:
        log(f"{name}: answer did not match the schema ({'; '.join(errs)}), asking once more")
        p = call(f"{full}\n\nYour previous answer did not match the schema: {'; '.join(errs)}. Do the same work and answer "
                 "again with only the JSON.")
        data, res = parse_result(p.stdout)
        errs = schema_errors(schema, data) if data else ["no JSON"]
        if errs:
            raise RuntimeError(f"{name}: no valid answer: {'; '.join(errs)}")
    log(f"{name}: {time.time() - t0:.0f}s, ${res.get('total_cost_usd', 0):.2f}")
    return data


# ---------- repo and sandbox ----------

def checkout(repo: str, base_sha: str, branch: str, sid) -> Path:
    mirror = WORK / "repos" / repo.replace("/", "__")
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", f"https://github.com/{repo}.git", str(mirror)])
    else:
        run(["git", "fetch", "--quiet", "origin"], cwd=mirror)
    # A retry reuses the branch name: remove the worktree an earlier (failed) build left on it.
    current = None
    for line in run(["git", "worktree", "list", "--porcelain"], cwd=mirror).stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            current = line.split(" ", 1)[1]
        elif line == f"branch refs/heads/{branch}" and current:
            run(["git", "worktree", "remove", "--force", current], cwd=mirror, check=False)
    run(["git", "worktree", "prune"], cwd=mirror, check=False)
    wt = WORK / "builds" / f"{sid}-{datetime.now():%Y%m%d-%H%M%S}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--quiet", "-B", branch, str(wt), base_sha], cwd=mirror)
    return wt


def changed_files(wt: Path, base_sha: str) -> list[str]:
    tracked = run(["git", "diff", "--name-only", base_sha], cwd=wt).stdout.split()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.split()
    return sorted(set(tracked + untracked))


def guard(wt: Path, base_sha: str) -> list[str]:
    """Undo edits to protected or unexpected files. Returns what was undone."""
    undone = []
    for f in changed_files(wt, base_sha):
        if PROTECTED.match(f) or not ALLOWED.match(f):
            existed = run(["git", "cat-file", "-e", f"{base_sha}:{f}"], cwd=wt, check=False).returncode == 0
            if existed:
                run(["git", "checkout", base_sha, "--", f], cwd=wt)
            else:
                (wt / f).unlink(missing_ok=True)
            undone.append(f)
    return undone


def gates(wt: Path) -> dict:
    """Regenerate milestones.json and run all gates inside Docker: no network, limited CPU/memory/time."""
    cmd = ["docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "1g", "--pids-limit", "256",
           "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{wt}:/game", "-w", "/game", IMAGE, "sh", "-c",
           "node -e \"import('./game/sim.js').then(m=>process.stdout.write(JSON.stringify(m.milestones,null,2)+'\\\\n'))\" "
           "> milestones.json && node gates.mjs --json"]
    try:
        p = run(cmd, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "gates": [{"name": "sandbox", "ok": False, "log": "the checks took longer than 5 minutes"}]}
    last = (p.stdout.strip().splitlines() or [""])[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return {"ok": False, "gates": [{"name": "sandbox", "ok": False, "log": (p.stderr or p.stdout)[-2000:]}]}


def gate_feedback(g: dict) -> str:
    return "\n\n".join(f"### {x['name']}\n{x.get('log', '')}" for x in g["gates"] if not x["ok"])


def coauthor(login: str) -> str:
    try:
        uid = json.loads(run(["gh", "api", f"users/{login}"]).stdout)["id"]
        return f"Co-authored-by: {login} <{uid}+{login}@users.noreply.github.com>"
    except Exception:  # noqa: BLE001
        return f"Co-authored-by: {login} <{login}@users.noreply.github.com>"


# ---------- one build ----------

PROTOTYPE_NOTE = (
    "PROTOTYPE ROUND. This is not a change to an existing game: the player suggested a whole new game, and the repository "
    "holds the Evolvia template game. Turn it into a tiny first playable version of the idea: the core mechanic only, "
    "simple shapes, 1-3 milestones that a player reaches in a few minutes (minSeconds 20 or more). Players will vote "
    "for the best of three prototypes and then evolve the winner idea by idea, so small and fun beats complete. "
    "Replacing the template's rules, drawing, bot and tests is expected here.")


def resolve(repo: str, ref: str) -> str:
    return run(["git", "ls-remote", f"https://github.com/{repo}.git", ref]).stdout.split()[0]


def build_loop(wt: Path, base_sha: str, task: str, sid, report, feedback: str = "", gates_first: bool = False):
    """Builder -> guard -> gates (up to MAX_BUILD_ROUNDS) -> reviewer (up to MAX_REVIEW_ROUNDS).
    Returns (builder answer or None, None) on success, or (None, {"status": "failed"|"rejected", "reason"}).
    gates_first: run the gates before the first builder call and skip the builder when they already pass (merges)."""
    built, v = None, None
    for review in range(1, MAX_REVIEW_ROUNDS + 1):
        g = None
        if gates_first and review == 1 and not feedback:
            report("gates", "Running the tests and the bot playtest…")
            g = gates(wt)
            log(f"#{sid} gates before the builder: {'green' if g['ok'] else 'red'}")
            if not g["ok"]:
                feedback = f"The checks failed:\n{gate_feedback(g)}"
        if not (g and g["ok"]):
            for rnd in range(1, MAX_BUILD_ROUNDS + 1):
                if not feedback:
                    report("build", "Writing the code…")
                elif feedback.startswith("Code review"):
                    report("build", "Changing the code after the review…")
                else:
                    report("build", f"Fixing what the tests found (try {rnd} of {MAX_BUILD_ROUNDS})…")
                built = agent("builder", task + (f"\n\nFix this first:\n{feedback}" if feedback else ""), wt)
                if built["status"] == "blocked":
                    return None, {"status": "rejected", "reason": built.get("blocked") or "This can't be built within the game's rules."}
                undone = guard(wt, base_sha)
                report("gates", "Running the tests and the bot playtest…")
                g = gates(wt)
                log(f"#{sid} build round {rnd}: gates {'green' if g['ok'] else 'red'}{f', undid {undone}' if undone else ''}")
                if g["ok"] and not undone:
                    break
                feedback = (f"These files may not be changed and were reset: {', '.join(undone)}.\n" if undone else "") + \
                           (f"The checks failed:\n{gate_feedback(g)}" if not g["ok"] else "")
        if not g or not g["ok"]:
            return None, {"status": "failed", "reason": "The automatic checks kept failing.\n" + gate_feedback(g)[-400:]}
        if not changed_files(wt, base_sha):
            return None, {"status": "failed", "reason": "The builder made no changes."}

        report("review", "Reviewing the change…")
        summary = f"Builder summary: {built['summary']}\nChangelog for players: {built['changelog']}" if built else ""
        v = agent("reviewer", f"Base commit: {base_sha}\n\n{task}\n\n{summary}", wt)
        if v["approve"]:
            return built, None
        feedback = "Code review blocked the change:\n" + "\n".join(f"- {b}" for b in v["blocking"])
        log(f"#{sid} review {review} blocked")
    return None, {"status": "failed", "reason": "The review kept finding problems: " + "; ".join(v["blocking"])[:400]}


def build(job: dict, push: bool = True, report=lambda step, message: None) -> dict:
    """Returns {"status": "done", "sha", "changelog", "branch"[, "name"]} | {"status": "rejected"|"failed", "reason"}.
    report(step, message) is called at every step; the platform shows the message live on the idea's card."""
    proto = job.get("kind") == "prototype"
    sid, author = (f"p{job['idea']}" if proto else job["suggestion"]), job["author"]
    if proto and not job.get("base_sha"):
        job["base_sha"] = resolve(job["repo"], job.get("base_ref", "main"))
    branch = f"{'prototype' if proto else 'suggestion'}/{str(sid).lstrip('p')}-{re.sub(r'[^A-Za-z0-9-]', '', author)[:30]}"
    wt = checkout(job["repo"], job["base_sha"], branch, sid)
    constitution = (wt / "constitution.md").read_text()
    idea = (f"<player_idea author=\"@{author}\">\n{'A game where ' if proto else 'What if '}{job['title']}\n"
            f"{job.get('body', '')}\n</player_idea>")
    log(f"#{sid} {job['repo']}@{job['base_sha'][:7]} on {branch}: {job['title']}")

    report("classify", "Checking the idea…")
    empty = WORK / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    context = PROTOTYPE_NOTE if proto else f"Game: {job.get('game_name', job.get('game'))}"
    c = agent("classifier", f"{context}\n\nConstitution:\n{constitution}\n\n{idea}", empty)
    if not c["accept"]:
        log(f"#{sid} rejected by the classifier: {c['reason']}")
        return {"status": "rejected", "reason": c["reason"]}

    task = (f"{PROTOTYPE_NOTE + chr(10) + chr(10) if proto else ''}"
            f"Task (from the player's idea, already checked):\n{c['task']}\n\nOriginal idea, for its spirit only:\n{idea}\n\n"
            f"Constitution:\n{constitution}")
    built, err = build_loop(wt, job["base_sha"], task, sid, report)
    if err:
        return err

    report("save", "Saving the new version…")
    run(["git", "add", "-A"], cwd=wt)
    head = f"Prototype: {c.get('name') or job['title']}" if proto else f"What if {job['title']}"
    msg = (f"{head}\n\n{built['changelog']}\n\nSuggested by @{author} (Evolvia {'game idea' if proto else 'idea'} #{sid}). "
           f"Built by the Evolvia AI builder, checked by the gates and an AI reviewer.\n\n{coauthor(author)}")
    run(["git", "-c", "user.name=Evolvia builder", "-c", "user.email=evolvia@atabuilderslab.pl", "commit", "-q", "-m", msg], cwd=wt)
    sha = run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
    if push:
        run(["git", "push", "-q", "-u", "origin", branch], cwd=wt)
    log(f"#{sid} done: {sha[:7]} on {branch}{' (pushed)' if push else ' (not pushed)'}")
    out = {"status": "done", "sha": sha, "changelog": built["changelog"], "branch": branch, "worktree": str(wt)}
    if proto:
        out["name"] = (c.get("name") or job["title"])[:60]
    return out


MERGE_NOTE = (
    "MERGE. Several versions of this game, each built from a different player idea, are being combined into one "
    "version that keeps every feature. Don't add anything new and don't drop any feature. Where the versions touch the "
    "same code, combine the two behaviours so both work together (for example two keys, two effects, two rules). Keep "
    "every rule test of every version passing, and keep the bot able to reach the milestones.")


def merge_build(job: dict, push: bool = True, report=lambda step, message: None) -> dict:
    """Merge 2+ versions (git commits on their branches) into one: git merge one by one, the builder resolves conflicts
    with every version's changelog in hand, then the usual gates and review. Returns like build()."""
    mid, vs = job["merge"], job["versions"]
    base = vs[0]["sha"]
    branch = f"merge/{mid}"
    wt = checkout(job["repo"], base, branch, f"m{mid}")
    constitution = (wt / "constitution.md").read_text()
    described = "\n".join(f"- {v['id']} (idea by @{v.get('credited_login') or 'unknown'}): {v.get('changelog') or 'no description'}"
                          for v in vs)
    task = f"{MERGE_NOTE}\n\nThe versions being merged:\n{described}\n\nConstitution:\n{constitution}"
    git = ["git", "-c", "user.name=Evolvia builder", "-c", "user.email=evolvia@atabuilderslab.pl"]
    log(f"merge {mid}: {', '.join(v['id'] for v in vs)} on {branch}")

    for v in vs[1:]:
        report("merge", f"Merging {v['id']}…")
        p = run([*git, "merge", "--no-edit", "-m", f"Merge {v['id']} into the combined version", v["sha"]], cwd=wt, check=False)
        if p.returncode == 0:
            continue
        conflicted = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).stdout.split()
        if not conflicted:
            return {"status": "failed", "reason": f"git merge of {v['id']} failed: {(p.stderr or p.stdout)[-300:]}"}
        report("merge", f"Combining the code of {v['id']} where both versions changed it…")
        fix = agent("builder", f"{task}\n\nRight now `{v['id']}` is being merged and git could not combine these files: "
                               f"{', '.join(conflicted)}. They contain conflict markers (<<<<<<<, =======, >>>>>>>). Edit each "
                               "file so both sides' features work together and no marker is left.", wt)
        if fix["status"] == "blocked":
            return {"status": "failed", "reason": fix.get("blocked") or f"Could not combine {v['id']}."}
        left = run(["git", "grep", "-l", "-E", "^(<<<<<<<|>>>>>>>)( |$)"], cwd=wt, check=False).stdout.split()
        if left:
            return {"status": "failed", "reason": f"Conflict markers left in {', '.join(left)}"}
        run(["git", "add", "-A"], cwd=wt)
        run([*git, "commit", "-q", "--no-edit"], cwd=wt)

    built, err = build_loop(wt, base, task, f"m{mid}", report, gates_first=True)
    if err:
        return err
    report("save", "Saving the merged version…")
    run(["git", "add", "-A"], cwd=wt)
    authors = sorted({v["credited_login"] for v in vs if v.get("credited_login")})
    changelog = "Combines " + "; ".join((v.get("changelog") or v["id"]).split(" — idea by")[0].split(" Idea by")[0].rstrip(". ")
                                        for v in vs) + "."
    # Always one closing commit (empty when nothing was left to fix): it carries the changelog and credits every author.
    msg = (f"Merge: make {len(vs)} versions work together\n\n{changelog}\n\nMerge #{mid} requested by @{job['author']}.\n\n"
           + "\n".join(coauthor(a) for a in authors))
    run([*git, "commit", "-q", "--allow-empty", "-m", msg], cwd=wt)
    sha = run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
    if push:
        run(["git", "push", "-q", "-u", "origin", branch], cwd=wt)
    log(f"merge {mid} done: {sha[:7]} on {branch}")
    return {"status": "done", "sha": sha, "changelog": changelog[:1000], "branch": branch}


def create_game(job: dict) -> dict:
    """The round winner gets its own public repo <owner>/evolvia-<slug>, with the prototype commit as main."""
    repo = f"{job['owner']}/evolvia-{job['slug']}"
    if run(["gh", "repo", "view", repo], check=False).returncode != 0:
        run(["gh", "repo", "create", repo, "--public", "--description", job["description"][:300]])
    mirror = WORK / "repos" / job["source_repo"].replace("/", "__")
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", f"https://github.com/{job['source_repo']}.git", str(mirror)])
    run(["git", "fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=mirror)
    run(["git", "push", "--quiet", f"https://github.com/{repo}.git", f"{job['sha']}:refs/heads/main"], cwd=mirror)
    log(f"round {job['round']}: created {repo} at {job['sha'][:7]}")
    return {"status": "done", "repo": repo, "sha": job["sha"]}


# ---------- platform loop ----------

def api(method: str, path: str, **kw):
    r = httpx.request(method, PLATFORM + path, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def run_loop(once: bool) -> None:
    if not TOKEN:
        sys.exit("EVOLVIA_WORKER_TOKEN is not set")
    while True:
        try:  # Slack announcer for #evolvia (an event consumer); never stops the worker
            from .notify import notify
            notify(log=log)
        except Exception as e:  # noqa: BLE001
            log(f"notify failed: {e}")
        # Don't take a job we can't build: the gates need Docker (Docker Desktop may be closed).
        if run(["docker", "info"], check=False, timeout=30).returncode != 0:
            log("Docker is not running, skipping this check")
            if once:
                return
            time.sleep(120)
            continue
        if not subscription_ok():
            log("claude is not signed in with the Claude subscription (run `claude` and /login), skipping this check")
            if once:
                return
            time.sleep(300)
            continue
        job = api("POST", "/api/worker/next")["job"]
        if job:
            kind = job.get("kind", "suggestion")
            label = {"suggestion": f"#{job.get('suggestion')}", "prototype": f"prototype {job.get('idea')}",
                     "create_game": f"round {job.get('round')}", "merge": f"merge {job.get('merge')}"}.get(kind, kind)
            result_path = job.get("result") or f"/api/worker/{job['suggestion']}/result"
            progress_path = job.get("progress") or (f"/api/worker/{job['suggestion']}/progress" if kind == "suggestion" else None)

            def report(step: str, message: str) -> None:
                if not progress_path:
                    return
                try:
                    api("POST", progress_path, json={"step": step, "message": message})
                except Exception as e:  # noqa: BLE001 — progress is nice to have, never stop a build for it
                    log(f"{label} progress not sent: {e}")

            try:
                result = (create_game(job) if kind == "create_game" else merge_build(job, report=report) if kind == "merge"
                          else build(job, report=report))
            except Exception as e:  # noqa: BLE001 — any crash is reported as a failed build
                result = {"status": "failed", "reason": f"Worker crashed: {e}"[:500]}
                log(f"{label} crashed: {e}")
            payload = {k: result[k] for k in ("status", "sha", "changelog", "name", "repo", "reason") if k in result}
            answer = api("POST", result_path, json=payload)
            log(f"{label} -> platform: {json.dumps(answer)}")
        if once:
            return
        time.sleep(30 if not job else 1)


def main() -> None:
    p = argparse.ArgumentParser(prog="evolvia-worker")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    n = sub.add_parser("notify", help="post new platform events to Slack #evolvia")
    n.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("try")
    t.add_argument("--repo", required=True)
    t.add_argument("--ref", default="main")
    t.add_argument("--title", required=True)
    t.add_argument("--body", default="")
    t.add_argument("--author", default="pbrudny")
    t.add_argument("--push", action="store_true")
    args = p.parse_args()
    if args.cmd == "notify":
        from .notify import notify
        notify(dry_run=args.dry_run)
        return
    if not shutil.which("docker") or not shutil.which("claude"):
        sys.exit("needs docker and the claude CLI")
    if args.cmd == "run":
        run_loop(args.once)
    else:
        sha = run(["git", "ls-remote", f"https://github.com/{args.repo}.git", args.ref]).stdout.split()[0]
        job = {"suggestion": f"try{datetime.now():%H%M%S}", "game": args.repo.split("/")[-1], "repo": args.repo, "base_sha": sha,
               "title": args.title, "body": args.body, "author": args.author}
        print(json.dumps(build(job, push=args.push), indent=2))


if __name__ == "__main__":
    main()
