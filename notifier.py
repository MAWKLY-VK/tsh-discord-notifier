#!/usr/bin/env python3
"""Post TheSuperHackers/GeneralsGameCode activity to a Discord channel, in Arabic.

Events posted through a Discord webhook:
  * pull request opened, ready for review (left draft), merged, closed without merge
  * new releases (the weekly-YYYY-MM-DD builds) with an Arabic summary
  * a daily digest of the previous day's activity

Titles, PR description excerpts and release highlights are machine translated to
Arabic with the free MyMemory API (no key needed); the original English title is
kept in the card footer. Internal PRs (ci, build, test, style, refactor, chore,
docs) are not posted.

It polls the public GitHub REST API (GITHUB_TOKEN / GH_TOKEN is used when set,
for example inside GitHub Actions) and remembers what it already posted in
state.json, so nothing is posted twice. Only the Python standard library is
used.

Usage:
  python notifier.py                    normal run (reads DISCORD_WEBHOOK_URL)
  python notifier.py --dry-run          print what would be posted, post nothing
  python notifier.py --dry-run --latest 3
                                        preview the latest 3 PRs of each kind and
                                        the latest 3 releases
  python notifier.py --latest 1         post the latest of each kind (webhook test)

The first real run only records what already exists and posts nothing, unless
--latest is given.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "TheSuperHackers/GeneralsGameCode"
API = "https://api.github.com"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
USER_AGENT = "tsh-discord-notifier/2.0"
HERE = Path(__file__).resolve().parent

# Pull requests are listed by "recently updated"; we page back until we pass
# the floor (see below) but never further than this many pages of 100.
MAX_PULL_PAGES = 5
# How many already-posted PR numbers / release tags to remember per kind.
KEEP_IDS = 500
# How many translated titles to cache in state.json.
KEEP_TRANSLATIONS = 400
# The floor is the point in time before which nothing can still be new. It is
# kept an hour behind the newest update we have seen, and only moved forward
# when something was posted or it is older than FLOOR_REFRESH, so state.json
# does not change (and get committed) on every quiet run.
FLOOR_OVERLAP = timedelta(hours=1)
FLOOR_REFRESH = timedelta(hours=12)

# Conventional-commit types of PRs that matter little to players.
DEFAULT_SKIP_TYPES = "ci,build,test,style,refactor,chore,docs"

COLOR_OPENED = 0x2EA043
COLOR_READY = 0x388BFD
COLOR_MERGED = 0x8250DF
COLOR_CLOSED = 0x8B949E
COLOR_RELEASE = 0xD29922
COLOR_DIGEST = 0x5865F2

# Discord limits: 10 embeds and 6000 characters of embed text per message.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS_PER_MESSAGE = 5800

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+"
)
TITLE_RE = re.compile(r"^\s*([A-Za-z]+)(?:\(([^)]*)\))?!?:\s*(.+)$")

TYPE_LABELS = {
    "fix": "إصلاح",
    "bugfix": "إصلاح خلل",
    "feat": "ميزة",
    "perf": "أداء",
    "tweak": "تعديل",
    "unify": "توحيد",
    "refactor": "إعادة هيكلة",
    "build": "بناء",
    "ci": "CI",
    "test": "اختبار",
    "style": "تنسيق",
    "chore": "صيانة",
    "docs": "توثيق",
    "revert": "تراجع",
}

log = logging.getLogger("tsh-notifier")


class NotifierError(Exception):
    pass


@dataclass
class Event:
    kind: str  # "opened" | "ready" | "merged" | "closed" | "release"
    key: object  # PR number or release tag
    when: datetime
    item: dict  # the PR or release from the GitHub API
    skip: bool = False  # internal PR: remembered but not posted
    test: bool = False  # picked by --latest: posted but kept out of the digest
    embed: dict | None = None
    digest_title: str = ""

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
    """First few lines of a PR body, without code blocks, HTML, images and headings."""
    if not markdown:
        return ""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"```.*?(```|$)", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line and not line.startswith("#"))
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(None, 1)[0]
    if cut.count("`") % 2:
        cut += "`"
    return cut + " …"


def parse_title(title: str) -> tuple[str | None, str | None, str]:
    """Split "fix(lanapi): Delete ..." into ("fix", "lanapi", "Delete ...")."""
    match = TITLE_RE.match(title)
    if not match:
        return None, None, title.strip()
    return match.group(1).lower(), match.group(2), match.group(3).strip()


def is_internal(title: str, skip_types: set[str]) -> bool:
    type_, _, _ = parse_title(title)
    return type_ is not None and type_ in skip_types


# --------------------------------------------------------------------------
# Translation (free MyMemory API)
# --------------------------------------------------------------------------

# Parts of a title that must not be translated: links, code and names. They
# are swapped for ZQ<n> placeholders, which the translator leaves alone, and
# put back afterwards. Game terms get a fixed Arabic wording the same way.
PROTECT_PATTERNS = [
    r"https?://\S+",
    r"`[^`]+`",
    r"(?<![\w-])--?[A-Za-z][\w-]*",  # command line options such as -genDataDir
    r"\b\w+(?:::~?\w+)+(?:\(\))?",  # Class::method
    r"\b\w+\(\)",  # function()
    r"\b[\w/]+\.(?:h|hpp|c|cpp|inl|ini|big|str|csf|w3d|dds|tga|txt|json|cmake|yml|yaml|md|exe|dll)\b",
    r"\b\w*_\w+\b",  # SNAKE_CASE and snake_case
    r"\b[A-Za-z]*[a-z][A-Z]\w*\b",  # camelCase and PascalCase
    r"\b[A-Z]{2,}[a-z]\w*\b",  # AIPlayer
    r"\b\w+(?:/\w+)+\b",  # float/integer, paths
    r"#\d+",
    r"\b(?:Generals Online|Zero Hour|World ?Builder|Generals|ZH|Windows|Linux|macOS|Steam|GameSpy"
    r"|C&C|CnC|DirectX|Tracy|CMake|Bink|Miles|VC6|GitHub|Discord|LAN)\b",
]
GLOSSARY = [
    (r"\bpull requests\b|\bPRs\b", "طلبات الدمج"),
    (r"\bpull request\b|\bPR\b", "طلب الدمج"),
    (r"(?i:\bcrash(?:es|ed|ing)?\b)", "انهيار"),
    (r"(?i:\bsanity checks?\b)", "تحقق من الصحة"),
    (r"(?i:\bfog of war\b|\bshroud\b)", "ضباب الحرب"),
    (r"(?i:\bregistry\b)", "سجل Windows"),
    (r"(?i:\blobb(?:y|ies)\b)", "اللوبي"),
    (r"(?i:\bdesyncs?\b|\bmismatch(?:es)?\b)", "عدم التزامن"),
    (r"(?i:\bpathfind(?:ing|er)\b)", "إيجاد المسار"),
    (r"(?i:\bhotkeys?\b)", "اختصار لوحة المفاتيح"),
    (r"(?i:\breplays?\b)", "الإعادة"),
    (r"(?i:\bmemory leaks?\b)", "تسرّب الذاكرة"),
    # "retail" here means the original game release that multiplayer must stay compatible with.
    (r"(?i:\bretail\b)", "النسخة الأصلية (retail)"),
]
PLACEHOLDER_RE = re.compile(r"ZQ\s?(\d+)")


def protect(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def swap(pattern: str, replacement: str | None) -> None:
        nonlocal text

        def repl(match):
            tokens.append(match.group(0) if replacement is None else replacement)
            return f"ZQ{len(tokens) - 1}"

        text = re.sub(pattern, repl, text)

    for pattern in PROTECT_PATTERNS:
        swap(pattern, None)
    for pattern, arabic in GLOSSARY:
        swap(pattern, arabic)
    return text, tokens


def restore(text: str, tokens: list[str]) -> str | None:
    """Put the protected parts back; None when the translator lost one."""
    seen = set()

    def repl(match):
        index = int(match.group(1))
        if index >= len(tokens):
            return match.group(0)
        seen.add(index)
        return tokens[index]

    result = PLACEHOLDER_RE.sub(repl, text)
    return result if len(seen) == len(tokens) else None


class Translator:
    """English to Arabic, with a cache kept in state.json.

    Uses Google Cloud Translation when GOOGLE_TRANSLATE_API_KEY is set and
    MyMemory otherwise, or when Google fails. When MyMemory fails too
    (network, daily quota), translation is off for the rest of the run and
    cards fall back to the English text.
    """

    def __init__(self, enabled: bool, cache: dict, email: str | None, google_key: str | None = None):
        self.enabled = enabled
        self.cache = cache
        self.email = email
        self.google_key = google_key
        self.calls = 0

    @property
    def provider(self) -> str:
        if not self.enabled:
            return "off"
        return "Google" if self.google_key else "MyMemory"

    def __call__(self, text: str, cache: bool = True) -> str | None:
        text = " ".join(text.split())
        if not self.enabled or not text:
            return None
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        if key in self.cache:
            return self.cache[key]
        protected, tokens = protect(text)
        if re.search(r"[A-Za-z]{2,}", PLACEHOLDER_RE.sub("", protected)):
            try:
                translated = self.fetch(truncate(protected, 480))
            except NotifierError as error:
                log.warning("Translation is off for this run: %s", error)
                self.enabled = False
                return None
        else:
            translated = protected  # nothing left to translate
        result = restore(translated, tokens)
        if result is None:
            log.warning("Translation dropped a protected word, keeping English: %s", text)
            return None
        if cache:
            self.cache[key] = result
        return result

    def fetch(self, text: str) -> str:
        if self.google_key:
            try:
                return self.fetch_google(text)
            except NotifierError as error:
                log.warning("Google Translate failed, using MyMemory for this run: %s", error)
                self.google_key = None
        return self.fetch_mymemory(text)

    def fetch_google(self, text: str) -> str:
        body = json.dumps({"q": [text], "source": "en", "target": "ar", "format": "text"}).encode("utf-8")
        request = urllib.request.Request(
            GOOGLE_TRANSLATE_URL,
            data=body,
            method="POST",
            # The key goes in a header, never in the URL, so it cannot end up in a log line.
            headers={"Content-Type": "application/json", "X-goog-api-key": self.google_key,
                     "User-Agent": USER_AGENT},
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise NotifierError(f"HTTP {error.code}: {truncate(detail, 200)}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise NotifierError(f"request failed: {error}") from None
        try:
            return html.unescape(data["data"]["translations"][0]["translatedText"])
        except (KeyError, IndexError, TypeError):
            raise NotifierError("unexpected response") from None

    def fetch_mymemory(self, text: str) -> str:
        params = {"q": text, "langpair": "en|ar"}
        if self.email:
            params["de"] = self.email
        request = urllib.request.Request(
            f"{MYMEMORY_URL}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT}
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise NotifierError(f"MyMemory request failed: {error}") from None
        translated = (data.get("responseData") or {}).get("translatedText") or ""
        if str(data.get("responseStatus")) != "200" or data.get("quotaFinished") or "MYMEMORY WARNING" in translated:
            raise NotifierError(f"MyMemory refused ({data.get('responseStatus')}): {truncate(translated, 160)}")
        return html.unescape(translated)


def arabic_title(title: str, translate: Translator) -> str | None:
    """"fix(lanapi): Delete ..." -> "إصلاح (lanapi): حذف ...", or None."""
    type_, scope, subject = parse_title(title)
    translated = translate(subject)
    if not translated:
        return None
    label = TYPE_LABELS.get(type_, type_) if type_ else None
    if label and scope:
        return f"{label} ({scope}): {translated}"
    if label:
        return f"{label}: {translated}"
    return translated


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

PR_HEADINGS = {
    "opened": ("🟢 **طلب دمج جديد**", COLOR_OPENED, "created_at"),
    "ready": ("🔵 **أصبح جاهزًا للمراجعة**", COLOR_READY, "updated_at"),
    "merged": ("🟣 **تم الدمج في `{base}`**", COLOR_MERGED, "merged_at"),
    "closed": ("⚪ **أُغلق دون دمج**", COLOR_CLOSED, "closed_at"),
}


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


def pr_embed(kind: str, pr: dict, repo: str, translate: Translator) -> tuple[dict, str]:
    heading, color, time_field = PR_HEADINGS[kind]
    base = (pr.get("base") or {}).get("ref") or "main"
    lines = [heading.format(base=base)]
    if kind == "opened" and pr.get("draft"):
        lines.append("📝 مسودة (Draft)")
    labels = pr_labels(pr)
    if labels:
        lines.append(labels)
    if kind == "opened":
        body = excerpt(pr.get("body"), 280)
        if body:
            lines.append("")
            lines.append(translate(body, cache=False) or body)
    translated = arabic_title(pr["title"], translate)
    embed = {
        "author": pr_author(pr),
        "title": truncate(f"{translated} (#{pr['number']})" if translated else f"#{pr['number']} {pr['title']}", 256),
        "url": pr["html_url"],
        "description": truncate("\n".join(lines), 2000),
        "color": color,
        # With a translation the original English title goes to the small footer line.
        "footer": {"text": truncate(pr["title"], 2048) if translated else repo},
        "timestamp": pr.get(time_field) or pr["updated_at"],
    }
    return embed, truncate(translated or pr["title"], 120)


def changelog_entries(body: str | None) -> list[tuple[str | None, str, str | None]]:
    """(type, subject, PR number) for each "- fix(x): ... (#123)" line of a release."""
    entries = []
    for line in (body or "").splitlines():
        match = re.match(r"^\s*[-*]\s+(.+)$", line)
        if not match:
            continue
        text = match.group(1).strip()
        ref = re.search(r"\((?:#(\d+)|https://github\.com/[^)\s]+/pull/(\d+))\)\s*$", text)
        number = (ref.group(1) or ref.group(2)) if ref else None
        if ref:
            text = text[: ref.start()].strip()
        type_, _, subject = parse_title(text)
        entries.append((type_, subject, number))
    return entries


RELEASE_GROUPS = [
    ("feat", ("feat",), "✨ ميزات"),
    ("fix", ("fix", "bugfix"), "🐛 إصلاحات"),
    ("perf", ("perf",), "⚡ أداء"),
    ("tweak", ("tweak", "unify"), "🎛️ تعديلات"),
]
MAX_RELEASE_HIGHLIGHTS = 8


def release_embed(release: dict, repo: str, translate: Translator) -> tuple[dict, str]:
    tag = release["tag_name"]
    heading = "📦 **إصدار أسبوعي جديد**" if tag.startswith("weekly-") else "📦 **إصدار جديد**"
    if release.get("prerelease"):
        heading += " (Pre-release)"
    lines = [heading]
    entries = changelog_entries(release.get("body"))
    player_types = {type_ for _, types, _ in RELEASE_GROUPS for type_ in types}
    counts = []
    for _, types, label in RELEASE_GROUPS:
        count = sum(1 for type_, _, _ in entries if type_ in types)
        if count:
            counts.append(f"{label}: {count}")
    internal = sum(1 for type_, _, _ in entries if type_ not in player_types)
    if internal:
        counts.append(f"🔧 تغييرات داخلية: {internal}")
    if counts:
        lines.append(" · ".join(counts))
    highlights = []
    for _, types, _ in RELEASE_GROUPS:
        for type_, subject, number in entries:
            if type_ in types and len(highlights) < MAX_RELEASE_HIGHLIGHTS:
                text = translate(subject, cache=False) or subject
                ref = f" ([#{number}](https://github.com/{repo}/pull/{number}))" if number else ""
                highlights.append(f"• **{TYPE_LABELS.get(type_, type_)}:** {text}{ref}")
    if highlights:
        lines += ["", "**أبرز ما يهم اللاعبين:**", *highlights]
    rest = len(entries) - len(highlights)
    if rest > 0:
        lines.append(f"📄 باقي التغييرات ({rest}) في صفحة الإصدار")
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
    return embed, tag


def build_embed(event: Event, repo: str, translate: Translator) -> None:
    if event.kind == "release":
        event.embed, event.digest_title = release_embed(event.item, repo, translate)
    else:
        event.embed, event.digest_title = pr_embed(event.kind, event.item, repo, translate)


DIGEST_SECTIONS = [
    ("opened", "🟢 طلبات جديدة"),
    ("ready", "🔵 جاهزة للمراجعة"),
    ("merged", "🟣 دُمجت"),
    ("closed", "⚪ أُغلقت دون دمج"),
    ("release", "📦 إصدارات"),
]
MAX_DIGEST_ROWS = 10


def digest_embed(day: str, items: list, repo: str) -> dict:
    lines = []
    for kind, heading in DIGEST_SECTIONS:
        rows = [item for item in items if item[0] == kind]
        if not rows:
            continue
        lines.append(f"**{heading} ({len(rows)})**")
        for _, key, title in rows[:MAX_DIGEST_ROWS]:
            if kind == "release":
                lines.append(f"• [{key}](https://github.com/{repo}/releases/tag/{key})")
            else:
                lines.append(f"• [#{key}](https://github.com/{repo}/pull/{key}) {title}")
        if len(rows) > MAX_DIGEST_ROWS:
            lines.append(f"• … و{len(rows) - MAX_DIGEST_ROWS} أخرى")
        lines.append("")
    return {
        "title": f"📰 ملخص نشاط يوم {day}",
        "url": f"https://github.com/{repo}/pulls",
        "description": truncate("\n".join(lines).strip(), 4000),
        "color": COLOR_DIGEST,
        "footer": {"text": repo},
    }


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


def message_payload(embeds: list[dict]) -> dict:
    # allowed_mentions stops "@everyone" in a PR title from pinging the server.
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}


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

STATE_LISTS = {"opened": "opened", "merged": "merged", "closed": "closed", "release": "releases"}


def empty_state(repo: str) -> dict:
    return {"repo": repo, "floor": None}


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NotifierError(f"Could not read {path}: {error}") from None


def save_state(path: Path, state: dict) -> None:
    for name in [*STATE_LISTS.values(), "drafts"]:
        state[name] = state[name][-KEEP_IDS:]
    state["tr"] = dict(list(state["tr"].items())[-KEEP_TRANSLATIONS:])
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remember(state: dict, event: Event) -> None:
    if event.kind == "ready":
        if event.key in state["drafts"]:
            state["drafts"].remove(event.key)
        return
    seen = state[STATE_LISTS[event.kind]]
    if event.key not in seen:
        seen.append(event.key)


def add_seen(state: dict, kind: str, key) -> None:
    seen = state[STATE_LISTS[kind]]
    if key not in seen:
        seen.append(key)


def seed_state(state: dict, pulls: list[dict], releases: list[dict], kinds: set[str]) -> None:
    """Mark everything currently visible as already posted, for the given kinds."""
    for pr in sorted(pulls, key=lambda pr: pr["number"]):
        if "opened" in kinds:
            add_seen(state, "opened", pr["number"])
        if "merged" in kinds and pr.get("merged_at"):
            add_seen(state, "merged", pr["number"])
        if "closed" in kinds and pr.get("closed_at") and not pr.get("merged_at"):
            add_seen(state, "closed", pr["number"])
    if "release" in kinds:
        for release in sorted(releases, key=lambda release: release.get("published_at") or ""):
            add_seen(state, "release", release["tag_name"])


def update_drafts(state: dict, pulls: list[dict]) -> None:
    drafts = state["drafts"]
    for pr in sorted(pulls, key=lambda pr: pr["number"]):
        number = pr["number"]
        if pr["state"] == "open" and pr.get("draft"):
            if number not in drafts:
                drafts.append(number)
        elif pr["state"] != "open" and number in drafts:
            drafts.remove(number)


def pr_event(kind: str, pr: dict, skip_types: set[str]) -> Event:
    time_field = PR_HEADINGS[kind][2]
    return Event(kind, pr["number"], parse_time(pr.get(time_field) or pr["updated_at"]), pr,
                 skip=is_internal(pr["title"], skip_types))


def release_event(release: dict) -> Event:
    published = release.get("published_at") or release.get("created_at")
    return Event("release", release["tag_name"], parse_time(published), release)


def new_events(pulls, releases, state, floor, skip_types, track_ready, track_closed) -> list[Event]:
    opened = set(state["opened"])
    merged = set(state["merged"])
    closed = set(state["closed"])
    drafts = set(state["drafts"])
    released = set(state["releases"])

    def after_floor(value):
        return floor is None or parse_time(value) > floor

    events = []
    for pr in pulls:
        number = pr["number"]
        if number not in opened and after_floor(pr["created_at"]):
            events.append(pr_event("opened", pr, skip_types))
        elif track_ready and number in drafts and pr["state"] == "open" and not pr.get("draft"):
            events.append(pr_event("ready", pr, skip_types))
        if pr.get("merged_at") and number not in merged and after_floor(pr["merged_at"]):
            events.append(pr_event("merged", pr, skip_types))
        elif (track_closed and pr.get("closed_at") and not pr.get("merged_at")
              and number not in closed and after_floor(pr["closed_at"])):
            events.append(pr_event("closed", pr, skip_types))
    for release in releases:
        published = release.get("published_at") or release.get("created_at")
        if release["tag_name"] not in released and after_floor(published):
            events.append(release_event(release))
    return events


def latest_events(pulls, releases, count, skip_types) -> list[Event]:
    public = [pr for pr in pulls if not is_internal(pr["title"], skip_types)]

    def newest(items, field):
        return sorted((item for item in items if item.get(field)), key=lambda item: item[field], reverse=True)[:count]

    events = [pr_event("opened", pr, skip_types) for pr in newest(public, "created_at")]
    events += [pr_event("merged", pr, skip_types) for pr in newest(public, "merged_at")]
    events += [pr_event("closed", pr, skip_types)
               for pr in newest([pr for pr in public if not pr.get("merged_at")], "closed_at")]
    events += [release_event(release) for release in newest(releases, "published_at")]
    for event in events:
        event.test = True
    return events


def newest_seen(pulls, releases) -> datetime | None:
    times = [parse_time(pr["updated_at"]) for pr in pulls]
    times += [parse_time(release.get("published_at")) for release in releases]
    times = [value for value in times if value]
    return max(times) if times else None


def add_to_digest(digest: dict, events: list[Event], tz: timezone) -> None:
    for event in events:
        day = event.when.astimezone(tz).date().isoformat()
        if digest.get("last") and day <= digest["last"]:
            continue  # that day's digest was already posted
        rows = digest["days"].setdefault(day, [])
        if len(rows) < 200:
            rows.append([event.kind, event.key, event.digest_title])


def due_digests(digest: dict, today: str) -> list[str]:
    return sorted(day for day, rows in digest["days"].items() if day < today and rows)


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


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=f"Post {REPO} pull requests and releases to a Discord webhook, in Arabic."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Discord payloads instead of posting them; state is not changed")
    parser.add_argument("--latest", type=int, default=0, metavar="N",
                        help="also treat the latest N opened, merged and closed PRs and releases as new")
    parser.add_argument("--repo", default=os.environ.get("TSH_REPO", REPO), help="owner/name (default: %(default)s)")
    parser.add_argument("--state", type=Path, default=HERE / "state.json", help="state file (default: state.json)")
    parser.add_argument("--webhook-file", type=Path, default=HERE / "discord_webhook.txt",
                        help="file holding the webhook URL when DISCORD_WEBHOOK_URL is not set")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="append log lines to this file (used automatically under pythonw)")
    parser.add_argument("--no-translate", dest="translate", action="store_false",
                        default=env_flag("TRANSLATE", True), help="keep everything in English (env TRANSLATE=0)")
    parser.add_argument("--no-digest", dest="digest", action="store_false",
                        default=env_flag("DIGEST", True), help="no daily digest (env DIGEST=0)")
    parser.add_argument("--digest-utc-offset", type=float,
                        default=float(os.environ.get("DIGEST_UTC_OFFSET") or 3),
                        help="time zone of the daily digest in hours from UTC (default: %(default)s)")
    parser.add_argument("--skip-types", default=os.environ.get("SKIP_TYPES", DEFAULT_SKIP_TYPES),
                        help="PR title types that are not posted, comma separated (default: %(default)s; "
                             "empty posts everything)")
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
    skip_types = {name.strip().lower() for name in args.skip_types.split(",") if name.strip()}
    tz = timezone(timedelta(hours=args.digest_utc_offset))
    today = datetime.now(tz).date()

    state = load_state(args.state)
    seeding = state is None
    if seeding:
        state = empty_state(repo)
    # Kinds added in later versions start silently, like a first run.
    new_kinds = {kind for kind, name in STATE_LISTS.items() if name not in state}
    track_ready = "drafts" in state
    for name in [*STATE_LISTS.values(), "drafts"]:
        state.setdefault(name, [])
    state.setdefault("tr", {})
    state.setdefault("digest", {"last": (today - timedelta(days=1)).isoformat(), "days": {}})
    floor = parse_time(state.get("floor"))
    before = json.dumps(state, sort_keys=True)

    github = GitHub(repo, token)
    pulls = github.pulls(floor)
    releases = github.releases()
    log.info(
        "Fetched %d PRs and %d releases from %s (%s, rate limit left: %s)",
        len(pulls), len(releases), repo, "token" if token else "no token", github.rate_remaining,
    )

    events = [] if seeding else new_events(
        pulls, releases, state, floor, skip_types, track_ready, track_closed="closed" not in new_kinds
    )
    if args.latest > 0:
        keys = {(event.kind, event.key) for event in events}
        events += [
            event for event in latest_events(pulls, releases, args.latest, skip_types)
            if (event.kind, event.key) not in keys
        ]
    events.sort(key=lambda event: event.when)
    newest = newest_seen(pulls, releases)
    if seeding:
        seed_state(state, pulls, releases, set(STATE_LISTS))
        if newest:
            state["floor"] = format_time(newest - FLOOR_OVERLAP)
    elif new_kinds:
        seed_state(state, pulls, releases, new_kinds)

    translate = Translator(args.translate, state["tr"], os.environ.get("MYMEMORY_EMAIL") or None,
                           os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip() or None)
    to_post = [event for event in events if not event.skip]
    for event in to_post:
        build_embed(event, repo, translate)
    messages = batch_messages(to_post)
    skipped = len(events) - len(to_post)
    log.info("%d new event(s), %d internal skipped, %d translation request(s), translation: %s",
             len(to_post), skipped, translate.calls, translate.provider)

    if args.dry_run:
        digest = copy.deepcopy(state["digest"])
        if args.digest:
            add_to_digest(digest, [event for event in to_post if not event.test], tz)
        days = due_digests(digest, today.isoformat()) if args.digest else []
        if seeding and not events:
            log.info("No %s yet: a real run would record the current PRs and releases and post "
                     "nothing. Add --latest N to preview messages.", args.state.name)
        log.info("Dry run: %d Discord message(s) and %d digest(s); nothing is posted.", len(messages), len(days))
        for index, message in enumerate(messages, 1):
            print(f"===== Discord message {index}/{len(messages)}: "
                  + ", ".join(event.label for event in message) + " =====")
            print(json.dumps(message_payload([event.embed for event in message]), indent=2, ensure_ascii=False))
        for day in days:
            print(f"===== Daily digest {day} =====")
            print(json.dumps(message_payload([digest_embed(day, digest["days"][day], repo)]),
                             indent=2, ensure_ascii=False))
        return 0

    for event in events:
        if event.skip:
            remember(state, event)

    failed = None
    posted: list[Event] = []
    for index, message in enumerate(messages):
        if index:
            time.sleep(1)
        try:
            post_discord(webhook, message_payload([event.embed for event in message]))
        except NotifierError as error:
            failed = error
            break
        for event in message:
            remember(state, event)
        posted += message
        log.info("Posted: %s", ", ".join(event.label for event in message))

    if args.digest:
        digest = state["digest"]
        add_to_digest(digest, [event for event in posted if not event.test], tz)
        for day in due_digests(digest, today.isoformat()) if failed is None else []:
            try:
                post_discord(webhook, message_payload([digest_embed(day, digest["days"][day], repo)]))
            except NotifierError as error:
                failed = error
                break
            digest["days"].pop(day)
            digest["last"] = day
            log.info("Posted the daily digest for %s", day)

    update_drafts(state, pulls)
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
