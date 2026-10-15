#!/usr/bin/env python3
"""Post TheSuperHackers/GeneralsGameCode activity to a Discord channel.

Three kinds of events are posted through a Discord webhook:
  * newly opened pull requests
  * merged pull requests
  * new releases (the weekly-YYYY-MM-DD builds)

It polls the public GitHub REST API (GITHUB_TOKEN / GH_TOKEN is used when set,
for example inside GitHub Actions) and remembers what it already posted in
state.json, so nothing is posted twice. Only the Python standard library is
used.

Usage:
  python notifier.py                    normal run (reads DISCORD_WEBHOOK_URL)
  python notifier.py --dry-run          print what would be posted, post nothing
  python notifier.py --dry-run --latest 3
                                        preview the latest 3 PRs opened, 3 PRs
                                        merged and 3 releases
  python notifier.py --latest 1         post the latest of each kind (webhook test)

The first real run only records what already exists and posts nothing, unless
--latest is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "TheSuperHackers/GeneralsGameCode"
API = "https://api.github.com"
USER_AGENT = "tsh-discord-notifier/1.0"
HERE = Path(__file__).resolve().parent

# Pull requests are listed by "recently updated"; we page back until we pass
# the floor (see below) but never further than this many pages of 100.
MAX_PULL_PAGES = 5
# How many already-posted PR numbers / release tags to remember per kind.
KEEP_IDS = 500
# The floor is the point in time before which nothing can still be new. It is
# kept an hour behind the newest update we have seen, and only moved forward
# when something was posted or it is older than FLOOR_REFRESH, so state.json
# does not change (and get committed) on every quiet run.
FLOOR_OVERLAP = timedelta(hours=1)
FLOOR_REFRESH = timedelta(hours=12)

COLOR_OPENED = 0x2EA043
COLOR_MERGED = 0x8250DF
COLOR_RELEASE = 0xD29922

# Discord limits: 10 embeds and 6000 characters of embed text per message.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS_PER_MESSAGE = 5800

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+"
)

log = logging.getLogger("tsh-notifier")


class NotifierError(Exception):
    pass


@dataclass
class Event:
    kind: str  # "opened" | "merged" | "release"
    key: object  # PR number or release tag
    when: datetime
    embed: dict

    @property
    def label(self) -> str:
        return f"{self.kind} #{self.key}" if self.kind != "release" else f"release {self.key}"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def excerpt(markdown: str | None, limit: int) -> str:
    """First few lines of a PR body, without code blocks, HTML and images."""
    if not markdown:
        return ""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"```.*?(```|$)", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(None, 1)[0]
    if cut.count("`") % 2:
        cut += "`"
    return cut + " …"


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

class GitHub:
    def __init__(self, repo: str, token: str | None):
        self.repo = repo
        self.token = token
        self.rate_remaining: str | None = None

    def get(self, url: str):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(2):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    self.rate_remaining = response.headers.get("X-RateLimit-Remaining")
                    return json.load(response), response.headers
            except urllib.error.HTTPError as error:
                if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
                    reset = error.headers.get("X-RateLimit-Reset")
                    when = (
                        datetime.fromtimestamp(int(reset), timezone.utc).strftime("%H:%M UTC")
                        if reset else "later"
                    )
                    raise NotifierError(
                        f"GitHub API rate limit reached; it resets at {when}. "
                        "Run less often or set GITHUB_TOKEN."
                    ) from None
                if error.code >= 500 and attempt == 0:
                    time.sleep(3)
                    continue
                raise NotifierError(f"GitHub API error HTTP {error.code} for {url}") from None
            except urllib.error.URLError as error:
                if attempt == 0:
                    time.sleep(3)
                    continue
                raise NotifierError(f"Could not reach GitHub: {error.reason}") from None
        raise AssertionError("unreachable")

    @staticmethod
    def next_link(headers) -> str | None:
        for part in (headers.get("Link") or "").split(","):
            match = re.search(r'<([^>]+)>;\s*rel="next"', part)
            if match:
                return match.group(1)
        return None

    def pulls(self, floor: datetime | None) -> list[dict]:
        """Recently updated PRs (opened, merged or otherwise touched), newest first."""
        url = f"{API}/repos/{self.repo}/pulls?state=all&sort=updated&direction=desc&per_page=100"
        pulls: list[dict] = []
        for _ in range(MAX_PULL_PAGES):
            page, headers = self.get(url)
            pulls.extend(page)
            if not page or floor is None or parse_time(page[-1]["updated_at"]) < floor:
                return pulls
            url = self.next_link(headers)
            if not url:
                return pulls
        log.warning(
            "More than %d updated PRs since the last run; older events may be missed.",
            MAX_PULL_PAGES * 100,
        )
        return pulls

    def releases(self) -> list[dict]:
        page, _ = self.get(f"{API}/repos/{self.repo}/releases?per_page=10")
        return [release for release in page if not release.get("draft")]


# --------------------------------------------------------------------------
# Discord embeds
# --------------------------------------------------------------------------

def pr_author(pr: dict) -> dict:
    user = pr.get("user") or {}
    author = {"name": user.get("login") or "ghost"}
    if user.get("html_url"):
        author["url"] = user["html_url"]
    if user.get("avatar_url"):
        author["icon_url"] = user["avatar_url"]
    return author


def pr_labels(pr: dict) -> str:
    names = [label["name"] for label in pr.get("labels") or [] if label.get("name")]
    return " ".join(f"`{name}`" for name in names[:8])


def opened_embed(pr: dict, repo: str) -> dict:
    lines = ["🟢 **طلب دمج جديد (Pull Request)**"]
    if pr.get("draft"):
        lines.append("📝 مسودة (Draft)")
    labels = pr_labels(pr)
    if labels:
        lines.append(labels)
    body = excerpt(pr.get("body"), 280)
    if body:
        lines.append("")
        lines.append(body)
    return {
        "author": pr_author(pr),
        "title": truncate(f"#{pr['number']} {pr['title']}", 256),
        "url": pr["html_url"],
        "description": truncate("\n".join(lines), 1500),
        "color": COLOR_OPENED,
        "footer": {"text": repo},
        "timestamp": pr["created_at"],
    }


def merged_embed(pr: dict, repo: str) -> dict:
    base = (pr.get("base") or {}).get("ref") or "main"
    lines = [f"🟣 **تم الدمج في `{base}`**"]
    labels = pr_labels(pr)
    if labels:
        lines.append(labels)
    return {
        "author": pr_author(pr),
        "title": truncate(f"#{pr['number']} {pr['title']}", 256),
        "url": pr["html_url"],
        "description": "\n".join(lines),
        "color": COLOR_MERGED,
        "footer": {"text": repo},
        "timestamp": pr["merged_at"],
    }


def release_embed(release: dict, repo: str) -> dict:
    tag = release["tag_name"]
    heading = "📦 **إصدار أسبوعي جديد**" if tag.startswith("weekly-") else "📦 **إصدار جديد**"
    if release.get("prerelease"):
        heading += " (Pre-release)"
    lines = [heading, ""]
    changelog = [
        line for line in (release.get("body") or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    used = 0
    shown = 0
    for line in changelog:
        if used + len(line) > 1000:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    hidden = sum(1 for line in changelog[shown:] if line.lstrip().startswith(("-", "*")))
    if hidden:
        lines.append(f"… و {hidden} تغييرات أخرى في صفحة الإصدار")
    fields = []
    for asset in (release.get("assets") or [])[:6]:
        size_mb = asset.get("size", 0) / (1024 * 1024)
        fields.append({
            "name": truncate(asset["name"], 256),
            "value": f"[⬇️ تحميل ({size_mb:.1f} MB)]({asset['browser_download_url']})",
            "inline": True,
        })
    owner = repo.split("/")[0]
    embed = {
        "author": {
            "name": owner,
            "url": f"https://github.com/{owner}",
            "icon_url": f"https://github.com/{owner}.png",
        },
        "title": truncate(release.get("name") or tag, 256),
        "url": release["html_url"],
        "description": truncate("\n".join(lines), 4000),
        "color": COLOR_RELEASE,
        "footer": {"text": f"{repo} · {tag}"},
        "timestamp": release.get("published_at") or release.get("created_at"),
    }
    if fields:
        embed["fields"] = fields
    return embed


def embed_size(embed: dict) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len((embed.get("footer") or {}).get("text", ""))
    size += len((embed.get("author") or {}).get("name", ""))
    for field in embed.get("fields") or []:
        size += len(field["name"]) + len(field["value"])
    return size


def batch_messages(events: list[Event]) -> list[list[Event]]:
    """Group events into Discord messages within Discord's per-message limits."""
    messages: list[list[Event]] = []
    current: list[Event] = []
    current_size = 0
    for event in events:
        size = embed_size(event.embed)
        if current and (
            len(current) >= MAX_EMBEDS_PER_MESSAGE
            or current_size + size > MAX_EMBED_CHARS_PER_MESSAGE
        ):
            messages.append(current)
            current, current_size = [], 0
        current.append(event)
        current_size += size
    if current:
        messages.append(current)
    return messages


def message_payload(events: list[Event]) -> dict:
    # allowed_mentions stops "@everyone" in a PR title from pinging the server.
    return {"embeds": [event.embed for event in events], "allowed_mentions": {"parse": []}}


def post_discord(webhook: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
    for _ in range(5):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            if error.code == 429:
                try:
                    retry_after = float(json.loads(body).get("retry_after", 2))
                except (ValueError, AttributeError):
                    retry_after = 2.0
                log.info("Discord rate limit, waiting %.1fs", retry_after)
                time.sleep(retry_after + 0.5)
                continue
            # Never include the webhook URL in errors: it is a secret.
            raise NotifierError(f"Discord rejected the message: HTTP {error.code}: {body[:300]}") from None
        except urllib.error.URLError as error:
            raise NotifierError(f"Could not reach Discord: {error.reason}") from None
    raise NotifierError("Discord kept rate limiting the webhook; giving up for this run.")


# --------------------------------------------------------------------------
# Events and state
# --------------------------------------------------------------------------

STATE_LISTS = {"opened": "opened", "merged": "merged", "release": "releases"}


def empty_state(repo: str) -> dict:
    return {"repo": repo, "floor": None, "opened": [], "merged": [], "releases": []}


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NotifierError(f"Could not read {path}: {error}") from None
    for name in STATE_LISTS.values():
        state.setdefault(name, [])
    return state


def save_state(path: Path, state: dict) -> None:
    for name in STATE_LISTS.values():
        state[name] = state[name][-KEEP_IDS:]
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remember(state: dict, kind: str, key) -> None:
    seen = state[STATE_LISTS[kind]]
    if key not in seen:
        seen.append(key)


def seed_state(state: dict, pulls: list[dict], releases: list[dict]) -> None:
    """Mark everything currently visible as already posted."""
    for pr in sorted(pulls, key=lambda pr: pr["number"]):
        remember(state, "opened", pr["number"])
    for pr in sorted((pr for pr in pulls if pr.get("merged_at")), key=lambda pr: pr["merged_at"]):
        remember(state, "merged", pr["number"])
    for release in sorted(releases, key=lambda release: release.get("published_at") or ""):
        remember(state, "release", release["tag_name"])


def opened_event(pr: dict, repo: str) -> Event:
    return Event("opened", pr["number"], parse_time(pr["created_at"]), opened_embed(pr, repo))


def merged_event(pr: dict, repo: str) -> Event:
    return Event("merged", pr["number"], parse_time(pr["merged_at"]), merged_embed(pr, repo))


def release_event(release: dict, repo: str) -> Event:
    published = release.get("published_at") or release.get("created_at")
    return Event("release", release["tag_name"], parse_time(published), release_embed(release, repo))


def new_events(pulls, releases, state, floor, repo) -> list[Event]:
    opened = set(state["opened"])
    merged = set(state["merged"])
    released = set(state["releases"])
    events = []
    for pr in pulls:
        if pr["number"] not in opened and (floor is None or parse_time(pr["created_at"]) > floor):
            events.append(opened_event(pr, repo))
        if pr.get("merged_at") and pr["number"] not in merged and (
            floor is None or parse_time(pr["merged_at"]) > floor
        ):
            events.append(merged_event(pr, repo))
    for release in releases:
        published = parse_time(release.get("published_at") or release.get("created_at"))
        if release["tag_name"] not in released and (floor is None or published > floor):
            events.append(release_event(release, repo))
    return events


def latest_events(pulls, releases, count, repo) -> list[Event]:
    newest_opened = sorted(pulls, key=lambda pr: pr["created_at"], reverse=True)[:count]
    newest_merged = sorted(
        (pr for pr in pulls if pr.get("merged_at")), key=lambda pr: pr["merged_at"], reverse=True
    )[:count]
    newest_releases = sorted(
        releases, key=lambda release: release.get("published_at") or "", reverse=True
    )[:count]
    return (
        [opened_event(pr, repo) for pr in newest_opened]
        + [merged_event(pr, repo) for pr in newest_merged]
        + [release_event(release, repo) for release in newest_releases]
    )


def newest_seen(pulls, releases) -> datetime | None:
    times = [parse_time(pr["updated_at"]) for pr in pulls]
    times += [parse_time(release.get("published_at")) for release in releases]
    times = [value for value in times if value]
    return max(times) if times else None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_webhook(webhook_file: Path) -> str:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and webhook_file.exists():
        lines = [line.strip() for line in webhook_file.read_text(encoding="utf-8-sig").splitlines()]
        webhook = next((line for line in lines if line and not line.startswith("#")), "")
    if not webhook:
        raise NotifierError(
            "No Discord webhook: set DISCORD_WEBHOOK_URL (a repository secret in GitHub Actions) "
            f"or put the URL in {webhook_file.name}. Use --dry-run to test without one."
        )
    if not WEBHOOK_RE.match(webhook):
        raise NotifierError("The Discord webhook URL does not look like https://discord.com/api/webhooks/<id>/<token>.")
    return webhook


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=f"Post {REPO} pull requests and releases to a Discord webhook."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Discord payloads instead of posting them; state is not changed")
    parser.add_argument("--latest", type=int, default=0, metavar="N",
                        help="also treat the latest N opened PRs, merged PRs and releases as new")
    parser.add_argument("--repo", default=os.environ.get("TSH_REPO", REPO), help="owner/name (default: %(default)s)")
    parser.add_argument("--state", type=Path, default=HERE / "state.json", help="state file (default: state.json)")
    parser.add_argument("--webhook-file", type=Path, default=HERE / "discord_webhook.txt",
                        help="file holding the webhook URL when DISCORD_WEBHOOK_URL is not set")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="append log lines to this file (used automatically under pythonw)")
    return parser.parse_args(argv)


def setup_logging(log_file: Path | None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if log.handlers:
        return
    if log_file is None and sys.stderr is None:  # pythonw.exe has no console
        log_file = HERE / "notifier.log"
    if log_file is not None:
        if log_file.exists() and log_file.stat().st_size > 1_000_000:
            os.replace(log_file, log_file.with_suffix(".old.log"))
        handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def run(args) -> int:
    repo = args.repo
    webhook = None if args.dry_run else load_webhook(args.webhook_file)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None

    state = load_state(args.state)
    seeding = state is None
    if seeding:
        state = empty_state(repo)
    floor = parse_time(state.get("floor"))
    before = json.dumps(state, sort_keys=True)

    github = GitHub(repo, token)
    pulls = github.pulls(floor)
    releases = github.releases()
    log.info(
        "Fetched %d PRs and %d releases from %s (%s, rate limit left: %s)",
        len(pulls), len(releases), repo, "token" if token else "no token", github.rate_remaining,
    )

    events = [] if seeding else new_events(pulls, releases, state, floor, repo)
    if args.latest > 0:
        keys = {(event.kind, event.key) for event in events}
        events += [
            event for event in latest_events(pulls, releases, args.latest, repo)
            if (event.kind, event.key) not in keys
        ]
    events.sort(key=lambda event: event.when)
    messages = batch_messages(events)

    newest = newest_seen(pulls, releases)
    if seeding:
        seed_state(state, pulls, releases)
        if newest:
            state["floor"] = format_time(newest - FLOOR_OVERLAP)

    if args.dry_run:
        if seeding and not events:
            log.info("No %s yet: a real run would record the current PRs and releases and post "
                     "nothing. Add --latest N to preview messages.", args.state.name)
        else:
            log.info("Dry run: %d event(s) in %d Discord message(s); nothing is posted.",
                     len(events), len(messages))
        for index, message in enumerate(messages, 1):
            print(f"===== Discord message {index}/{len(messages)}: "
                  + ", ".join(event.label for event in message) + " =====")
            print(json.dumps(message_payload(message), indent=2, ensure_ascii=False))
        return 0

    failed = None
    for index, message in enumerate(messages):
        if index:
            time.sleep(1)
        try:
            post_discord(webhook, message_payload(message))
        except NotifierError as error:
            failed = error
            break
        for event in message:
            remember(state, event.kind, event.key)
        log.info("Posted: %s", ", ".join(event.label for event in message))

    if failed is None and not seeding and newest:
        candidate = newest - FLOOR_OVERLAP
        if floor is None or (candidate > floor and (events or candidate - floor > FLOOR_REFRESH)):
            state["floor"] = format_time(candidate)

    if json.dumps(state, sort_keys=True) != before:
        save_state(args.state, state)
        log.info("Saved %s", args.state.name)
    if seeding:
        log.info("First run: recorded %d PRs and %d releases as already seen.",
                 len(state["opened"]), len(state["releases"]))
    if failed is not None:
        raise failed
    if not events:
        log.info("Nothing new.")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_file)
    try:
        return run(args)
    except NotifierError as error:
        log.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
