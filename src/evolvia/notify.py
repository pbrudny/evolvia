"""Slack announcer for #evolvia in the EDP workspace: an event consumer (DESIGN.md §11.1).

Reads the platform's public event feed (/api/events) after a saved cursor and posts:
- a new idea ("What if…?") as a channel message,
- the decision on it (sent to the builder, rejected with the reason, failed) as a reply in the idea's thread,
- the new version (changelog + play link) in the thread, also sent to the channel,
plus idea rounds, prototypes, new games and merges.

    evolvia-worker notify [--dry-run]     (the build worker also runs it on every poll)

State (cursor, idea -> thread) is kept in ~/.evolvia-worker/slack.json. The first run only sets the cursor to the
latest event, so old history is never posted.
"""

import json
import os
import subprocess
from pathlib import Path

import httpx

PLATFORM = os.environ.get("EVOLVIA_URL", "https://evolvia.atabuilderslab.pl").rstrip("/")
CHANNEL = os.environ.get("EVOLVIA_SLACK_CHANNEL", "evolvia")
STATE = Path(os.environ.get("EVOLVIA_WORK", Path.home() / ".evolvia-worker")) / "slack.json"
WITH_SECRETS = Path(__file__).resolve().parents[3] / "tools" / "with_secrets" / "with-secrets"
TOKEN_KEY = "SLACK_EDP_USER_TOKEN"


def _load_token() -> str | None:
    """The Slack token from the environment, else from the sops store via with-secrets (only this key)."""
    if os.environ.get(TOKEN_KEY):
        return os.environ[TOKEN_KEY]
    if not WITH_SECRETS.exists():
        return None
    p = subprocess.run([str(WITH_SECRETS), "--only", TOKEN_KEY, "--export"], capture_output=True, text=True, timeout=60)
    for line in p.stdout.splitlines():
        key, _, value = line.removeprefix("export ").partition("=")
        if key.strip() == TOKEN_KEY:
            return value.strip().strip("'\"")
    return None


def esc(text) -> str:
    """Player text is never Slack markup: no mentions, links or formatting injection."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Slack:
    def __init__(self, token: str, dry_run: bool = False):
        self.token, self.dry = token, dry_run

    def call(self, method: str, **data) -> dict:
        r = httpx.post(f"https://slack.com/api/{method}", data=data, timeout=30,
                       headers={"Authorization": f"Bearer {self.token}"}).json()
        if not r.get("ok"):
            raise RuntimeError(f"slack {method}: {r.get('error')}")
        return r

    def channel_id(self, name: str) -> str:
        cursor = ""
        while True:
            r = self.call("conversations.list", types="public_channel", limit="500", exclude_archived="true", cursor=cursor)
            for c in r["channels"]:
                if c["name"] == name:
                    return c["id"]
            cursor = r.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                raise RuntimeError(f"no channel #{name}")

    def post(self, channel: str, text: str, thread: str | None = None, broadcast: bool = False) -> str | None:
        if self.dry:
            print(f"[{'thread ' + thread if thread else 'channel'}] {text}\n")
            return thread or "dry"
        data = {"channel": channel, "text": text, "unfurl_links": "false", "unfurl_media": "false"}
        if thread:
            data["thread_ts"] = thread
            if broadcast:
                data["reply_broadcast"] = "true"
        return self.call("chat.postMessage", **data)["ts"]


def play(game: str, version: str) -> str:
    return f"{PLATFORM}/g/{game}/v/{version}"


def render(e: dict, names: dict) -> tuple[str, str | None, bool] | None:
    """(text, thread key, broadcast) for an event, or None to skip it. Thread key "s<id>" = a suggestion's thread."""
    d, actor, t = e["data"], e["actor"], e["type"]
    game = names.get(d.get("game"), d.get("game"))
    if t == "SuggestionSubmitted":
        return (f":bulb: *New idea for {esc(game)}* from @{esc(actor)}:\n>What if {esc(d.get('title'))}\n"
                f"Vote for it after reaching a milestone: {play(d['game'], d['version'])}#suggestions", None, False)
    if t == "SuggestionAccepted":
        why = (f"Claude picked it: top idea with {d.get('votes')} votes" if actor == "claude"
               else "The instructor sent it to the builder")
        return f":hammer_and_wrench: {why}. The AI builder is on it.", f"s{d['suggestion']}", False
    if t == "SuggestionRejected":
        return f":no_entry_sign: Not built: {esc(d.get('reason')) or 'rejected'}", f"s{d['suggestion']}", False
    if t == "BuildFailed" and d.get("suggestion"):
        return (f":warning: The build failed: {esc(d.get('reason'))[:300]}\nThe instructor can retry it.",
                f"s{d['suggestion']}", False)
    if t == "BuildSucceeded":
        log = names.get(("changelog", d.get("version")), "")
        return (f":tada: *New version of {esc(game)} is live*, idea by @{esc(d.get('author'))}!\n{esc(log)}\n"
                f"Play it: {play(d['game'], d['version'])}", f"s{d['suggestion']}", True)
    if t == "VersionsMerged":
        log = names.get(("changelog", d.get("version")), "")
        return (f":twisted_rightwards_arrows: *Merged version of {esc(game)}*: {len(d.get('versions', []))} versions in one.\n"
                f"{esc(log)}\nPlay it: {play(d['game'], d['version'])}", None, False)
    if t == "VersionPromoted":
        return f":star: The main version of {esc(game)} is now `{esc(d.get('version'))}`: {play(d['game'], d['version'])}", None, False
    if t == "IdeaRoundOpened":
        return (f":crystal_ball: *Idea round open: {esc(d.get('title'))}*. Post your game idea and vote: "
                f"{PLATFORM}/rounds/{d['round']}", None, False)
    if t == "PrototypeBuilt":
        return f":video_game: A prototype is ready to play: {PLATFORM}{d.get('url', '')}", None, False
    if t == "GameCreated" and actor == "worker":
        return (f":trophy: *{esc(game)} won the round* (idea by @{esc(d.get('author'))}) and is now a game: "
                f"{PLATFORM}/g/{d['game']}", None, False)
    return None


def notify(dry_run: bool = False, log=print) -> int:
    """Post new events. Returns how many messages were posted."""
    token = _load_token()
    if not token:
        log("notify: no Slack token, skipping")
        return 0
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    events = httpx.get(f"{PLATFORM}/api/events", params={"after": state.get("cursor", 0), "limit": 500}, timeout=30).json()
    if "cursor" not in state:  # first run: start from now
        state = {"cursor": events[-1]["id"] if events else 0, "threads": {}}
        if not dry_run:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
        log(f"notify: started at event {state['cursor']}")
        return 0
    if not events:
        return 0
    slack = Slack(token, dry_run)
    channel = state.get("channel") or slack.channel_id(CHANNEL)
    state["channel"] = channel
    names = {g["slug"]: g["name"] for g in _games()}
    posted = 0
    for e in events:
        if e["type"] == "VersionDeployed":  # remember the changelog for BuildSucceeded / VersionsMerged
            names[("changelog", e["data"].get("version"))] = e["data"].get("changelog", "")
        msg = render(e, names)
        if msg:
            text, key, broadcast = msg
            thread = state["threads"].get(key) if key else None
            ts = slack.post(channel, text, thread=thread, broadcast=broadcast and bool(thread))
            if e["type"] == "SuggestionSubmitted":
                state["threads"][f"s{e['data']['suggestion']}"] = ts
            posted += 1
        state["cursor"] = e["id"]
        if not dry_run:
            STATE.write_text(json.dumps(state))
    if posted:
        log(f"notify: posted {posted} message(s) to #{CHANNEL}")
    return posted


def _games() -> list[dict]:
    try:
        return httpx.get(f"{PLATFORM}/api/games", timeout=30).json()
    except (httpx.HTTPError, ValueError):
        return []
