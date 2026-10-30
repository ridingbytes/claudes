#!/usr/bin/env python3
"""List, find, resume and audit Claude Code sessions.

Reads the transcripts under ~/.claude/projects and the registry of
running sessions under ~/.claude/sessions. Parsed metadata is cached
per transcript, keyed by mtime and size, so only changed transcripts
are read again.

Usage:
  claudes [list] [QUERY...] [FILTERS]   list sessions
  claudes pick [QUERY...] [FILTERS]     fzf picker, Enter resumes
  claudes -r [ID] [--fork]              resume, as with claude -r
  claudes show ID...|-                  details of sessions
  claudes rename ID NAME                set the session title
  claudes rm [ID...|-] [FILTERS]        move sessions to the Trash
  claudes grep TEXT [FILTERS]           full text search
  claudes audit [QUERY...] [FILTERS]    secrets and risky actions
  claudes review ID|DIR [FOCUS...]      start /security-review there
  claudes memory [ACTION]               list, show, grep, dupes,
                                        promote, rm, edit memories

ID is a session id prefix or (part of) a session title. When it
matches several sessions, a picker opens to choose one. "-" reads
ids from stdin, so selections pipe from one command to the next:

  claudes --unnamed --older 30d -q | claudes rm -
"""

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

__version__ = "0.3.0"

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE, "projects")
REGISTRY = os.path.join(CLAUDE, "sessions")
TRASH = os.path.join(HOME, ".Trash", "claude-sessions")
CACHE = os.path.join(HOME, ".cache", "claude-sessions.json")
AUDIT_CACHE = os.path.join(HOME, ".cache", "claude-sessions-audit.json")
GLOBAL_CLAUDE_MD = os.path.join(CLAUDE, "CLAUDE.md")
# Sits next to the project memories but is not loaded by Claude Code
USER_MEMORY = os.path.join(CLAUDE, "memory")

# Bump when the cached Meta or audit findings change shape or meaning
CACHE_VERSION = 4
AUDIT_VERSION = 7

# Below this many transcripts to scan, a process pool costs more than
# it saves
PARALLEL_THRESHOLD = 4


# Data model

@dataclass
class Meta:
    """What one transcript tells about its session; cached as JSON."""

    cwd: str | None = None
    branch: str | None = None
    first: str | None = None
    last: str | None = None
    titles: dict = field(default_factory=dict)
    first_prompt: str | None = None
    last_prompt: str | None = None
    prompts: int = 0
    continued_in: str | None = None
    cost: float | None = None
    prs: list = field(default_factory=list)
    entrypoints: list = field(default_factory=list)
    kind: str | None = None


@dataclass
class Process:
    """A running Claude Code process, from the session registry."""

    pid: int
    kind: str = "interactive"
    status: str = "?"
    entrypoint: str | None = None

    @property
    def is_background(self):
        return self.kind == "bg"

    @property
    def label(self):
        prefix = "bg" if self.is_background else "run"
        return f"{prefix}:{self.status}"


@dataclass
class Session(Meta):
    """A transcript with its metadata and, if running, its process."""

    id: str = ""
    path: str = ""
    size: int = 0
    title: str = ""
    title_source: str | None = None
    process: Process | None = None

    @property
    def short_id(self):
        return self.id[:8]

    @property
    def is_running(self):
        return self.process is not None

    @property
    def is_named(self):
        """True when the title is one you gave, not a generated one."""
        return self.title_source in NAMED_SOURCES

    @property
    def is_hidden(self):
        """Empty and superseded sessions are hidden unless running."""
        if self.is_running:
            return False
        return self.prompts == 0 or self.continued_in is not None

    @property
    def all_entrypoints(self):
        """Entrypoints from the transcript plus the live process."""
        found = list(self.entrypoints)
        live = self.process.entrypoint if self.process else None
        if live and live not in found:
            found.append(live)
        return found

    @property
    def origin(self):
        """Where the session was started: app, term, or another name."""
        found = self.all_entrypoints
        return entrypoint_label(found[0]) if found else "?"

    @property
    def source(self):
        """Origin followed by later environments, such as term>app."""
        labels = [entrypoint_label(e) for e in self.all_entrypoints]
        label = ">".join(labels) or "?"
        return label + " bg" if self.kind == "bg" else label


@dataclass
class Finding:
    """One audit hit; repeated hits in a session raise the count."""

    group: str
    category: str
    severity: str
    detail: str
    fp: str
    where: str = ""
    time: str | None = None
    count: int = 1

    @property
    def key(self):
        return (self.group, self.category, self.fp)


@dataclass
class Filters:
    """Which sessions a command works on, and in which order."""

    query: list = field(default_factory=list)
    include_hidden: bool = False
    named: bool | None = None
    origin: str | None = None
    directory: str | None = None
    under: str | None = None
    older: float | None = None
    newer: float | None = None
    ids: list | None = None
    sort: str = "active"
    reverse: bool = False
    limit: int = 0

    @classmethod
    def from_args(cls, args, **overrides):
        """Build filters from parsed command line options."""
        under = os.getcwd() if args.here else full_path(args.under)
        values = {
            "query": getattr(args, "query", []),
            "include_hidden": args.all,
            "named": args.named,
            "origin": args.origin,
            "directory": full_path(args.dir),
            "under": under,
            "older": args.older,
            "newer": args.newer,
            "ids": read_ids(args.ids_from) if args.ids_from else None,
            "sort": args.sort,
            "reverse": args.reverse,
            "limit": args.limit,
        }
        values.update(overrides)
        return cls(**values)

    @property
    def selects(self):
        """True when any filter narrows the sessions down."""
        narrowing = (self.query, self.named is not None, self.origin,
                     self.directory, self.under, self.older, self.newer,
                     self.ids is not None)
        return any(narrowing)


TITLE_TYPES = {
    "custom-title": ("customTitle", "user"),
    "agent-name": ("agentName", "agent"),
    "ai-title": ("aiTitle", "ai"),
}
TITLE_SOURCE_LABELS = {
    "user": "your name",
    "agent": "agent name",
    "ai": "AI title",
    "prompt": "first prompt",
}
NAMED_SOURCES = ("user", "agent")
ENTRYPOINT_LABELS = {"claude-desktop": "app", "cli": "term"}

# Sort key name -> (key function, descending by default)
SORT_KEYS = {
    "active": (lambda s: s.last or "", True),
    "started": (lambda s: s.first or "", True),
    "name": (lambda s: s.title.lower(), False),
    "dir": (lambda s: (s.cwd or "").lower(), False),
    "msgs": (lambda s: s.prompts, True),
    "size": (lambda s: s.size, True),
    "cost": (lambda s: s.cost or 0, True),
}


def entrypoint_label(entrypoint):
    """Return app or term for known entrypoints, else the raw name."""
    return ENTRYPOINT_LABELS.get(entrypoint, entrypoint)


def full_path(path):
    """Return an absolute path with ~ expanded, or None."""
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(path))


DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
RE_DURATION = re.compile(r"^(\d+)([mhdw])$")


def parse_duration(text):
    """Return seconds for a duration like 30m, 12h, 30d or 2w."""
    match = RE_DURATION.match(text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a duration like 30m, 12h, 30d or 2w")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


# A session id or the start of one, as the first word of a line
RE_ID_TOKEN = re.compile(r"^[0-9a-f][0-9a-f-]{3,35}$")


def id_token(line):
    """Return the session id at the start of a line, or None.

    Accepts plain ids from -q as well as whole table rows, whose
    first column is the short id.
    """
    words = line.split()
    if not words or not RE_ID_TOKEN.match(words[0]):
        return None
    return words[0]


def read_ids(source):
    """Return the session ids in a file, or on stdin for "-"."""
    if source == "-":
        return [t for t in map(id_token, sys.stdin) if t]
    with open(source) as handle:
        return [t for t in map(id_token, handle) if t]


# Terminal output

class Output:
    """Color setting: None follows the terminal, True/False force it."""

    color = None


def use_color():
    """Return True when output should carry ANSI colors."""
    if Output.color is not None:
        return Output.color
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


@contextlib.contextmanager
def forced_color():
    """Render with colors regardless of where output goes (for fzf)."""
    previous = Output.color
    Output.color = True
    try:
        yield
    finally:
        Output.color = previous


def paint(text, code):
    """Wrap text in an ANSI color code when color is enabled."""
    if not use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text):
    """Return text in the dim style."""
    return paint(text, "2")


def clip(text, width):
    """Fit text into width characters, marking a cut with an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "\u2026"


def clip_left(text, width):
    """Fit text into width, cutting at the start: ends matter in paths."""
    if len(text) <= width:
        return text.ljust(width)
    return "\u2026" + text[len(text) - width + 1:]


def short_path(path):
    """Replace the home directory prefix with a tilde."""
    if path and (path == HOME or path.startswith(HOME + os.sep)):
        return "~" + path[len(HOME):]
    return path or "?"


def is_interactive():
    """Return True when a person sits at the terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def fail(message):
    """Print an error message and exit with status 1."""
    print("claudes: " + message, file=sys.stderr)
    sys.exit(1)


class Progress:
    """A one line counter on stderr, shown only on a terminal."""

    def __init__(self, label, total):
        self.label = label
        self.total = total
        self.enabled = sys.stderr.isatty() and total > 1

    def update(self, done):
        if self.enabled:
            print(f"\r{self.label} {done}/{self.total}", end="",
                  file=sys.stderr, flush=True)

    def finish(self):
        if self.enabled:
            print("\r\033[K", end="", file=sys.stderr, flush=True)


# Time

def parse_iso(value):
    """Return an aware datetime for an ISO timestamp, or None."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_age(value):
    """Return a compact relative age such as 5m, 3h, 2d or a date."""
    stamp = parse_iso(value)
    if stamp is None:
        return "?"
    now = datetime.datetime.now(datetime.timezone.utc)
    seconds = max((now - stamp).total_seconds(), 0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 86400 * 14:
        return f"{int(seconds // 86400)}d"
    return stamp.astimezone().strftime("%d.%m.%y")


def format_local(value):
    """Return a timestamp as local date and time."""
    stamp = parse_iso(value)
    if stamp is None:
        return "?"
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


# Transcript parsing
#
# Transcripts reach hundreds of megabytes. Most lines are API request
# blobs or tool output that carry nothing for the list, so cheap string
# checks decide which lines are worth decoding as JSON at all.

RE_TIMESTAMP = re.compile(r'"timestamp":"([^"]+)"')
RE_ENTRYPOINT = re.compile(r'"entrypoint":"([^"]+)"')
RE_KIND = re.compile(r'"sessionKind":"([^"]+)"')
RE_CWD = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')
RE_BRANCH = re.compile(r'"gitBranch":"((?:[^"\\]|\\.)*)"')
RE_COMMAND = re.compile(r"<command-name>([^<]+)</command-name>")
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$")

SKIPPED_TYPED_RECORDS = ('{"type":"api-request', '{"type":"file-history',
                         '{"type":"attachment"')
PROMPT_LIMIT = 500


def parse_record(line):
    """Return the JSON object of one transcript line, or None."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def message_text(content):
    """Return the plain text of a message content field."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [block.get("text", "") for block in content
             if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(parts)


def record_text(record):
    """Return the message text of a user or assistant record."""
    return message_text((record.get("message") or {}).get("content"))


def prompt_text(record):
    """Return the human prompt of a user record, or None."""
    if record.get("isMeta") or record.get("isCompactSummary"):
        return None
    text = record_text(record).strip()
    command = RE_COMMAND.search(text)
    if command:
        return command.group(1).strip()
    if not text or text.startswith("<"):
        return None
    return text


def is_typed_line(line):
    """Return True for small records that carry session metadata."""
    return (line.startswith('{"type":"')
            and not line.startswith(SKIPPED_TYPED_RECORDS))


def is_prompt_line(line):
    """Return True for user lines that may hold a human prompt."""
    return '"type":"user"' in line and '"tool_result"' not in line


def apply_typed_record(meta, record):
    """Merge a title, link or cost record into the metadata."""
    kind = record.get("type")
    if kind in TITLE_TYPES:
        key, source = TITLE_TYPES[kind]
        if record.get(key):
            meta.titles[source] = record[key]
    elif kind == "continued-in":
        meta.continued_in = record.get("continuedInSessionId")
    elif kind == "cost-state":
        meta.cost = record.get("totalCostUSD")
    elif kind == "pr-link":
        url = record.get("prUrl")
        if url and url not in meta.prs:
            meta.prs.append(url)


def apply_user_record(meta, record):
    """Count a human prompt and remember the first and last one."""
    text = prompt_text(record)
    if text is None:
        return
    meta.prompts += 1
    meta.first_prompt = meta.first_prompt or text[:PROMPT_LIMIT]
    meta.last_prompt = text[:PROMPT_LIMIT]


def apply_timestamp(meta, line):
    """Widen the first/last activity span by the line's timestamp."""
    match = RE_TIMESTAMP.search(line)
    if not match:
        return
    stamp = match.group(1)
    # ISO timestamps in one format compare correctly as strings
    if meta.first is None or stamp < meta.first:
        meta.first = stamp
    if meta.last is None or stamp > meta.last:
        meta.last = stamp


def apply_context(meta, line):
    """Pick up directory, branch, entrypoint and kind from a line."""
    if meta.cwd is None:
        cwd = RE_CWD.search(line)
        if cwd:
            meta.cwd = json.loads('"' + cwd.group(1) + '"')
    branch = RE_BRANCH.search(line)
    if branch:
        meta.branch = branch.group(1)
    entrypoint = RE_ENTRYPOINT.search(line)
    if entrypoint and entrypoint.group(1) not in meta.entrypoints:
        meta.entrypoints.append(entrypoint.group(1))
    kind = RE_KIND.search(line)
    if kind:
        meta.kind = kind.group(1)


def apply_line(meta, line):
    """Update the metadata from one raw transcript line."""
    record = None
    if is_typed_line(line) or is_prompt_line(line):
        record = parse_record(line)
    if record and record.get("type") == "user":
        apply_user_record(meta, record)
    elif record:
        apply_typed_record(meta, record)
    if '"timestamp"' in line:
        apply_timestamp(meta, line)
        apply_context(meta, line)


def read_meta(path):
    """Parse one transcript and return its metadata."""
    meta = Meta()
    with open(path, errors="replace") as handle:
        for line in handle:
            apply_line(meta, line)
    return meta


# Cache

def file_key(path):
    """Return what invalidates a cache entry: mtime and size."""
    stat = os.stat(path)
    return [stat.st_mtime_ns, stat.st_size]


def read_json(path):
    """Return the parsed JSON of a file, or None if unreadable."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_private_json(path, data):
    """Write JSON atomically, readable by the owner only.

    The caches hold prompt text and masked findings, so they are
    created with mode 0600 instead of following the umask.
    """
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(temp, flags, 0o600), "w") as handle:
        json.dump(data, handle)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


class TranscriptCache:
    """JSON data per transcript, valid while its file key matches."""

    def __init__(self, path, version):
        self.path = path
        self.version = version
        data = read_json(path)
        valid = isinstance(data, dict) and data.get("version") == version
        self.entries = data.get("entries", {}) if valid else {}
        self.changed = not valid

    def get(self, transcript, key):
        """Return cached data for a transcript, or None if stale."""
        entry = self.entries.get(transcript)
        if entry and entry.get("key") == key:
            return entry["data"]
        return None

    def put(self, transcript, key, data):
        """Store data computed from the transcript as it was at key."""
        self.entries[transcript] = {"key": key, "data": data}
        self.changed = True

    def save(self):
        """Write the cache, dropping transcripts that no longer exist."""
        alive = {path: entry for path, entry in self.entries.items()
                 if os.path.exists(path)}
        if not self.changed and len(alive) == len(self.entries):
            return
        write_private_json(self.path, {"version": self.version,
                                       "entries": alive})


# Session index

def iter_transcripts():
    """Yield the paths of all top level session transcripts."""
    if not os.path.isdir(PROJECTS):
        return
    for slug in sorted(os.listdir(PROJECTS)):
        project = os.path.join(PROJECTS, slug)
        if not os.path.isdir(project):
            continue
        for entry in sorted(os.listdir(project)):
            if entry.endswith(".jsonl") and RE_UUID.match(entry[:-6]):
                yield os.path.join(project, entry)


def load_meta(path, cache):
    """Return the metadata of a transcript, from the cache if fresh."""
    # The key is taken before reading, so a transcript that grows
    # while it is parsed is parsed again next time
    key = file_key(path)
    data = cache.get(path, key)
    if data is not None:
        return Meta(**data)
    meta = read_meta(path)
    cache.put(path, key, dataclasses.asdict(meta))
    return meta


def pick_title(meta):
    """Return (title, source) with the best available title."""
    for source in ("user", "agent", "ai"):
        if meta.titles.get(source):
            return meta.titles[source], source
    if meta.first_prompt:
        return meta.first_prompt, "prompt"
    return "", None


RE_NOT_SLUG = re.compile(r"[^A-Za-z0-9]")


def slugify(name):
    """Encode a path the way Claude Code names project folders."""
    return RE_NOT_SLUG.sub("-", name)


def list_directories(base):
    """Return the names of the directories in base, or []."""
    try:
        return sorted(e.name for e in os.scandir(base) if e.is_dir())
    except OSError:
        return []


def walk_slug(base, rest):
    """Return (deepest existing path, part of the slug left over).

    Walks the file system one level at a time and follows entries
    whose own slug starts the remaining slug, so dots, dashes and
    umlauts that all became "-" are recovered from the real names.
    """
    if not rest:
        return base, ""
    best = (base, rest)
    for entry in list_directories(base):
        encoded = slugify(entry)
        if rest != encoded and not rest.startswith(encoded + "-"):
            continue
        path, left = walk_slug(os.path.join(base, entry),
                               rest[len(encoded) + 1:])
        if not left:
            return path, ""
        if len(left) < len(best[1]):
            best = (path, left)
    return best


@functools.cache
def decode_slug(slug):
    """Return the directory a project folder name stands for.

    When the directory is gone, the existing part is resolved and
    the rest of the folder name is appended as it is.
    """
    path, left = walk_slug(os.sep, slug.lstrip("-"))
    return os.path.join(path, left) if left else path


def build_session(path, meta, process=None):
    """Combine a transcript path, its metadata and its process."""
    title, source = pick_title(meta)
    values = dataclasses.asdict(meta)
    slug = os.path.basename(os.path.dirname(path))
    values["cwd"] = meta.cwd or decode_slug(slug)
    return Session(**values, id=os.path.basename(path)[:-6], path=path,
                   size=os.path.getsize(path), title=title,
                   title_source=source, process=process)


def load_sessions():
    """Return all sessions, most recently active first."""
    cache = TranscriptCache(CACHE, CACHE_VERSION)
    processes = load_processes()
    sessions = []
    for path in iter_transcripts():
        session_id = os.path.basename(path)[:-6]
        meta = load_meta(path, cache)
        sessions.append(build_session(path, meta, processes.get(session_id)))
    cache.save()
    sessions.sort(key=lambda s: s.last or "", reverse=True)
    return sessions


# Running sessions

def pid_alive(pid):
    """Return True when a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_registry_entry(path):
    """Return (session id, Process) of a live registry entry, or None."""
    entry = read_json(path)
    if not isinstance(entry, dict):
        return None
    pid = entry.get("pid")
    session_id = entry.get("sessionId")
    if not isinstance(pid, int) or not session_id or not pid_alive(pid):
        return None
    process = Process(pid=pid, kind=entry.get("kind") or "interactive",
                      status=entry.get("status") or "?",
                      entrypoint=entry.get("entrypoint"))
    return session_id, process


def load_processes():
    """Return running Claude Code processes keyed by session id."""
    if not os.path.isdir(REGISTRY):
        return {}
    found = {}
    for name in os.listdir(REGISTRY):
        if not name.endswith(".json"):
            continue
        entry = read_registry_entry(os.path.join(REGISTRY, name))
        if entry:
            found[entry[0]] = entry[1]
    return found


def process_tty(pid):
    """Return the controlling terminal of a process, or None."""
    result = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                            capture_output=True, text=True)
    tty = result.stdout.strip()
    return tty if tty and tty != "??" else None


def process_detail(process):
    """Return pid, kind, status and terminal of a process."""
    parts = [f"pid {process.pid}", process.kind, process.status]
    tty = process_tty(process.pid)
    if tty:
        parts.append("tty " + tty)
    return ", ".join(parts)


# Selecting sessions

def search_text(session):
    """Return the lowercased text that queries match against."""
    fields = [session.title, session.cwd, session.id, session.branch,
              session.first_prompt, session.last_prompt]
    fields.extend(session.titles.values())
    return " ".join(f for f in fields if f).lower()


def matches_query(session, terms):
    """Return True when every query term occurs in the session."""
    if not terms:
        return True
    text = search_text(session)
    return all(term.lower() in text for term in terms)


def in_directory(session, directory):
    """Return True when the session ran in or below directory."""
    cwd = session.cwd or ""
    return cwd == directory or cwd.startswith(directory + os.sep)


def idle_seconds(session):
    """Return the seconds since the last activity of a session."""
    stamp = parse_iso(session.last)
    if stamp is None:
        return float("inf")
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - stamp).total_seconds()


def in_age_range(session, older, newer):
    """Return True when the idle time lies within older and newer."""
    if older is None and newer is None:
        return True
    idle = idle_seconds(session)
    return ((older is None or idle >= older)
            and (newer is None or idle <= newer))


def has_id(session, ids):
    """Return True when the session id starts with one of ids."""
    return ids is None or any(session.id.startswith(i) for i in ids)


def passes(session, filters):
    """Return True when a session passes every filter."""
    return all((
        filters.include_hidden or not session.is_hidden,
        filters.named is None or session.is_named == filters.named,
        filters.origin is None or session.origin == filters.origin,
        filters.directory is None or session.cwd == filters.directory,
        filters.under is None or in_directory(session, filters.under),
        in_age_range(session, filters.older, filters.newer),
        has_id(session, filters.ids),
        matches_query(session, filters.query),
    ))


def sort_sessions(sessions, key, reverse=False):
    """Return sessions ordered by one of the SORT_KEYS."""
    function, descending = SORT_KEYS[key]
    return sorted(sessions, key=function, reverse=descending != reverse)


def select_sessions(sessions, filters):
    """Return the filtered, sorted and limited sessions."""
    selected = [s for s in sessions if passes(s, filters)]
    selected = sort_sessions(selected, filters.sort, filters.reverse)
    return selected[: filters.limit] if filters.limit else selected


# Finding one session

def find_candidates(sessions, ref, newest=False):
    """Return the sessions a reference could mean.

    An id prefix wins, then an exact title, then a part of a title.
    With newest set, a title shared by several sessions means the most
    recently active one; sessions arrive sorted that way.
    """
    ref = ref.lower()
    by_id = [s for s in sessions if s.id.startswith(ref)]
    if len(by_id) == 1:
        return by_id
    exact = [s for s in sessions if s.title.lower() == ref]
    if exact and (newest or len(exact) == 1):
        return exact[:1]
    if by_id or exact:
        return by_id or exact
    return [s for s in sessions if ref in s.title.lower()]


def report_ambiguous(ref, candidates):
    """List the candidates for an ambiguous reference and exit."""
    print(f"claudes: {ref!r} matches {len(candidates)} sessions:",
          file=sys.stderr)
    for session in candidates[:20]:
        print("  " + format_row(session), file=sys.stderr)
    print("Use a longer id prefix or the full title.", file=sys.stderr)
    sys.exit(1)


def resolve(sessions, ref, newest=False):
    """Return the one session ref means; let the user pick if needed."""
    candidates = find_candidates(sessions, ref, newest)
    if not candidates:
        fail(f"no session matches {ref!r}")
    if len(candidates) == 1:
        return candidates[0]
    if not can_pick():
        report_ambiguous(ref, candidates)
    chosen = fzf_select(candidates,
                        header=f"{ref!r} matches several sessions")
    if chosen is None:
        sys.exit(1)
    return chosen


def resolve_id(sessions, token):
    """Return the session an id prefix from a pipe names.

    Piped input is matched by id only and never opens a picker: a
    script must not act on a session nobody chose.
    """
    found = [s for s in sessions if s.id.startswith(token)]
    if len(found) != 1:
        problem = "no session" if not found else f"{len(found)} sessions"
        fail(f"piped id {token!r} matches {problem}")
    return found[0]


def sessions_from_refs(sessions, refs, newest=False):
    """Return the sessions for refs, where "-" reads ids from stdin."""
    found = []
    for ref in refs:
        if ref == "-":
            found += [resolve_id(sessions, t) for t in read_ids("-")]
        else:
            found.append(resolve(sessions, ref, newest))
    return list({session.id: session for session in found}.values())


# Table layout

FIXED_WIDTHS = {"id": 8, "active": 8, "status": 9, "source": 8, "msgs": 4}
GAP = "  "
PIPE_WIDTH = 130


@dataclass
class Layout:
    """Widths of the two flexible columns."""

    title: int
    directory: int


def table_layout():
    """Split the terminal width left by fixed columns: title, directory."""
    if sys.stdout.isatty():
        width = shutil.get_terminal_size((PIPE_WIDTH, 24)).columns
    else:
        width = PIPE_WIDTH
    fixed = sum(FIXED_WIDTHS.values()) + len(GAP) * 6
    room = max(width - fixed, 40)
    title = room * 11 // 20
    return Layout(title=title, directory=room - title)


def title_cell(session, width):
    """Return the title: bold if you named it, dim if only a prompt."""
    text = clip(session.title, width)
    if session.title_source == "user":
        return paint(text, "1")
    if session.title_source == "prompt":
        return dim(text)
    return text


def status_cell(session):
    """Return the running state, green, or blank."""
    width = FIXED_WIDTHS["status"]
    if not session.is_running:
        return " " * width
    return paint(clip(session.process.label, width), "32")


def format_row(session, layout=None):
    """Return one table line for a session."""
    layout = layout or table_layout()
    return GAP.join([
        paint(session.short_id, "33"),
        format_age(session.last).rjust(FIXED_WIDTHS["active"]),
        status_cell(session),
        paint(clip(session.source, FIXED_WIDTHS["source"]), "35"),
        title_cell(session, layout.title),
        paint(clip(short_path(session.cwd), layout.directory), "36"),
        str(session.prompts).rjust(FIXED_WIDTHS["msgs"]),
    ])


def header_row(layout=None):
    """Return the column header line."""
    layout = layout or table_layout()
    return paint(GAP.join([
        "id".ljust(FIXED_WIDTHS["id"]),
        "active".rjust(FIXED_WIDTHS["active"]),
        "status".ljust(FIXED_WIDTHS["status"]),
        "source".ljust(FIXED_WIDTHS["source"]),
        "title".ljust(layout.title),
        "directory".ljust(layout.directory),
        "msgs".rjust(FIXED_WIDTHS["msgs"]),
    ]), "4")


def session_json(session):
    """Return the public fields of a session for --json output."""
    return {
        "id": session.id,
        "title": session.title,
        "title_source": session.title_source,
        "directory": session.cwd,
        "branch": session.branch,
        "source": session.source,
        "started": session.first,
        "active": session.last,
        "prompts": session.prompts,
        "cost": session.cost,
        "size": session.size,
        "running": session.process.label if session.process else None,
        "continued_in": session.continued_in,
        "prs": session.prs,
        "path": session.path,
    }


# fzf picker

CLIPBOARD_COMMANDS = ("pbcopy", "wl-copy", "xclip -selection clipboard")


def can_pick():
    """Return True when an interactive fzf picker can be shown."""
    return is_interactive() and shutil.which("fzf") is not None


def clipboard_command():
    """Return a shell command that copies stdin, or None."""
    for command in CLIPBOARD_COMMANDS:
        if shutil.which(command.split()[0]):
            return command
    return None


def own_command():
    """Return a shell word that runs this script again."""
    return shlex.quote(os.path.realpath(sys.argv[0]))


def fzf_command(query, header):
    """Return the fzf command line for the session picker."""
    copy = clipboard_command()
    hints = "Enter: choose" + ("   ctrl-y: copy id" if copy else "")
    command = [
        "fzf", "--ansi", "--no-sort", "--layout=reverse",
        "--delimiter", "\t", "--with-nth", "2..", "--header-lines", "1",
        "--prompt", "session> ", "--query", query,
        "--header", f"{header}\n{hints}" if header else hints,
        "--preview", f"CLICOLOR_FORCE=1 {own_command()} show {{1}}",
        "--preview-window", "down,45%,wrap",
    ]
    if copy:
        command += ["--bind", f"ctrl-y:execute-silent(printf %s {{1}} "
                              f"| {copy})"]
    return command


def fzf_select(sessions, query="", header=""):
    """Let the user choose one session with fzf; None on cancel."""
    with forced_color():
        layout = table_layout()
        lines = ["\t" + header_row(layout)]
        lines += [f"{s.id}\t{format_row(s, layout)}" for s in sessions]
    result = subprocess.run(fzf_command(query, header),
                            input="\n".join(lines), text=True,
                            stdout=subprocess.PIPE)
    chosen = result.stdout.split("\t", 1)[0].strip()
    return next((s for s in sessions if s.id == chosen), None)


# Commands: browsing

def print_list_footer(shown, total_visible, total):
    """Tell how many sessions are shown and how many are hidden."""
    parts = [f"{shown} of {total_visible} sessions"]
    hidden = total - total_visible
    if hidden:
        parts.append(f"{hidden} empty or continued hidden (-a)")
    print(dim(", ".join(parts)))


def format_session(session, template):
    """Fill a --format template with the fields of a session."""
    fields = session_json(session)
    fields["short_id"] = session.short_id
    try:
        return template.format_map(fields)
    except (KeyError, ValueError) as error:
        fail(f"bad --format {template!r} ({error}); fields: "
             + ", ".join(fields))


def cmd_list(args, sessions):
    """Print the session table, or ids, a template, or JSON."""
    filters = Filters.from_args(args)
    selected = select_sessions(sessions, filters)
    if args.json:
        json.dump([session_json(s) for s in selected], sys.stdout,
                  indent=2)
        print()
        return
    template = "{id}" if args.ids else args.format
    if template:
        for session in selected:
            print(format_session(session, template))
        return
    interactive = sys.stdout.isatty()
    layout = table_layout()
    if interactive:
        print(header_row(layout))
    for session in selected:
        print(format_row(session, layout))
    if interactive:
        unlimited = dataclasses.replace(filters, limit=0)
        visible = len(select_sessions(sessions, unlimited))
        print_list_footer(len(selected), visible, len(sessions))


def cmd_pick(args, sessions):
    """Choose a session with fzf and resume it."""
    if not can_pick():
        fail("pick needs fzf and a terminal")
    # The query goes to fzf as an editable search instead of a filter
    filters = Filters.from_args(args, query=[])
    chosen = fzf_select(select_sessions(sessions, filters),
                        query=" ".join(args.query))
    if chosen:
        resume_session(chosen, fork=False)


def detail_rows(session):
    """Return (label, value) pairs for the details view."""
    source = TITLE_SOURCE_LABELS.get(session.title_source, "none")
    cost = f"${session.cost:.2f}" if session.cost else None
    rows = [
        ("id", session.id),
        ("title", f"{session.title}  {dim('(' + source + ')')}"),
        ("directory", short_path(session.cwd)),
        ("source", session.source),
        ("branch", session.branch),
        ("started", format_local(session.first)),
        ("active", format_local(session.last)),
        ("prompts", session.prompts),
        ("size", f"{session.size / 1e6:.1f} MB"),
        ("cost", cost),
        ("running", session.process and process_detail(session.process)),
        ("continued", session.continued_in),
        ("file", short_path(session.path)),
    ]
    rows.extend(("pr", url) for url in session.prs)
    rows.append(("resume", f"claudes -r {session.short_id}"))
    return rows


def print_details(session):
    """Print every known detail of one session."""
    for label, value in detail_rows(session):
        if value not in (None, "", []):
            print(f"{dim(label.ljust(10))} {value}")
    for label, text in (("first prompt", session.first_prompt),
                        ("last prompt", session.last_prompt)):
        print("\n" + dim(label))
        print(text or "-")


def cmd_show(args, sessions):
    """Print details of the given sessions."""
    for number, session in enumerate(
            sessions_from_refs(sessions, args.refs, newest=True)):
        if number:
            print("\n" + dim("-" * 40) + "\n")
        print_details(session)


# Commands: resume and review

def enter_directory(path):
    """Change into a session directory, or exit with an explanation."""
    if not os.path.isdir(path):
        fail(f"directory is gone: {short_path(path)}\n"
             "Recreate it, then resume again.")
    os.chdir(path)
    print("cd " + short_path(path), file=sys.stderr)


def resume_argv(session, fork):
    """Return the claude command line that reopens a session."""
    if session.process and session.process.is_background:
        return ["claude", "attach", session.id]
    argv = ["claude", "--resume", session.id]
    return argv + ["--fork-session"] if fork else argv


def resume_session(session, fork):
    """Replace this process with claude resuming the session."""
    process = session.process
    if process and not process.is_background and not fork:
        fail(f"session is open in another terminal "
             f"({process_detail(process)}).\n"
             "Switch to it, or use --fork to branch off a copy.")
    enter_directory(session.cwd)
    argv = resume_argv(session, fork)
    os.execvp(argv[0], argv)


def cmd_resume(args, sessions):
    """Resume a session given by reference, or picked with fzf."""
    if args.ref:
        session = resolve(sessions, args.ref, newest=True)
    elif can_pick():
        visible = select_sessions(sessions, Filters())
        session = fzf_select(visible)
    else:
        fail("which session? Give an id prefix or a title.")
    if session:
        resume_session(session, args.fork)


def review_target(sessions, ref):
    """Return (directory, label) for a session or directory ref."""
    if os.path.isdir(ref):
        path = os.path.abspath(ref)
        return path, os.path.basename(path)
    session = resolve(sessions, ref, newest=True)
    return session.cwd, session.title or session.short_id


def is_git_repo(path):
    """Return True when path lies inside a git work tree."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True)
    return result.returncode == 0


def cmd_review(args, sessions):
    """Start a new claude session running /security-review."""
    path, label = review_target(sessions, args.ref)
    if os.path.isdir(path) and not is_git_repo(path):
        fail("not a git repository: " + short_path(path))
    enter_directory(path)
    prompt = " ".join(["/security-review"] + args.focus)
    name = "security-review " + clip(label, 40).strip()
    os.execvp("claude", ["claude", "-n", name, prompt])


# Commands: managing

def ensure_idle(session, action):
    """Exit when a session is running and must not be changed."""
    if session.is_running:
        fail(f"cannot {action} {session.short_id} while it runs "
             f"({process_detail(session.process)})")


def cmd_rename(args, sessions):
    """Append a custom-title record, as /rename does."""
    session = resolve(sessions, args.ref)
    ensure_idle(session, "rename")
    record = {"type": "custom-title", "customTitle": args.name,
              "sessionId": session.id}
    with open(session.path, "a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    print(f"renamed {session.short_id} to {args.name!r}")


def ask(question):
    """Return the answer typed at the terminal, even behind a pipe.

    When stdin carries piped ids, the question goes to /dev/tty, so
    "claudes -q ... | claudes rm -" still asks before it moves.
    """
    if sys.stdin.isatty():
        return input(question)
    with open("/dev/tty", "r+") as tty:
        tty.write(question)
        tty.flush()
        return tty.readline()


def confirm(question):
    """Ask a yes/no question on the terminal; default is no."""
    try:
        answer = ask(question + " [y/N] ")
    except (EOFError, OSError):
        fail("no terminal to confirm on; add -y to skip the question")
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def trash_session(session):
    """Move a transcript and its sidecar directory to the Trash."""
    project = os.path.basename(os.path.dirname(session.path))
    target = os.path.join(TRASH, project)
    os.makedirs(target, exist_ok=True)
    sidecar = session.path[: -len(".jsonl")]
    for path in (session.path, sidecar):
        if os.path.exists(path):
            shutil.move(path, os.path.join(target, os.path.basename(path)))


def removal_targets(args, sessions, filters):
    """Return the sessions rm should move, without duplicates.

    Named sessions, piped ids and filter matches add up. Filters see
    hidden sessions too: an old continued transcript is just as
    removable. Running sessions are skipped, not refused, when they
    only came in through a filter.
    """
    targets = sessions_from_refs(sessions, args.refs)
    for session in targets:
        ensure_idle(session, "remove")
    if filters.selects:
        targets += [s for s in select_sessions(sessions, filters)
                    if not s.is_running]
    if args.empty:
        targets += [s for s in sessions
                    if s.prompts == 0 and not s.is_running]
    return list({session.id: session for session in targets}.values())


def cmd_rm(args, sessions):
    """Move sessions to the Trash after confirmation."""
    filters = Filters.from_args(args, include_hidden=True)
    if not args.refs and not args.empty and not filters.selects:
        fail("which sessions? Give ids, titles, - for piped ids, "
             "filters such as --older 30d, or --empty.")
    targets = removal_targets(args, sessions, filters)
    if not targets:
        print("nothing to remove")
        return
    layout = table_layout()
    for session in targets:
        print(format_row(session, layout))
    if not args.yes and not confirm(f"Move {len(targets)} to the Trash?"):
        return
    for session in targets:
        trash_session(session)
    print(f"moved {len(targets)} session(s) to {short_path(TRASH)}")


# Commands: grep

def snippet(text, needle, width=120):
    """Return the text around the first occurrence of needle."""
    index = text.lower().find(needle)
    if index < 0:
        return None
    start = max(index - 40, 0)
    return " ".join(text[start: start + width].split())


def highlight(text, needle):
    """Mark every occurrence of needle in text."""
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return pattern.sub(lambda m: paint(m.group(0), "1;33"), text)


def line_snippet(line, needle):
    """Return a snippet from a user or assistant line, or None."""
    record = parse_record(line)
    if not record or record.get("type") not in ("user", "assistant"):
        return None
    return snippet(record_text(record), needle)


def grep_session(session, needle, limit):
    """Return up to limit snippets of needle in one transcript."""
    hits = []
    with open(session.path, errors="replace") as handle:
        for line in handle:
            if needle not in line.lower():
                continue
            found = line_snippet(line, needle)
            if found:
                hits.append(found)
            if len(hits) >= limit:
                break
    return hits


def cmd_grep(args, sessions):
    """Search the message text of the selected transcripts."""
    needle = " ".join(args.text).lower()
    filters = Filters.from_args(args, include_hidden=True)
    layout = table_layout()
    matched = 0
    for session in select_sessions(sessions, filters):
        hits = grep_session(session, needle, args.max)
        if not hits:
            continue
        matched += 1
        print(format_row(session, layout))
        for hit in hits:
            print("    " + dim("\u2026") + highlight(hit, needle)
                  + dim("\u2026"))
    if not matched:
        print(f"no session mentions {needle!r}", file=sys.stderr)


# Audit: patterns

# (category, severity, regex). Group 1, if present, is the secret.
SECRET_PATTERNS = [
    # The header alone is often just text about keys; a real key has
    # a base64 body, with newlines possibly escaped as \n in the JSON
    ("private key", "high", re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"
        r"(?:\s|\\n|\\r)+[A-Za-z0-9+/=]{40}")),
    ("anthropic key", "high", re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,})")),
    ("openai key", "high", re.compile(
        r"(sk-(?:proj-)?[A-Za-z0-9]{20,}[A-Za-z0-9_-]*)")),
    ("aws key", "high", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("github token", "high", re.compile(
        r"\b((?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}))")),
    ("slack token", "high", re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})")),
    ("google key", "high", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})")),
    ("stripe key", "high", re.compile(
        r"\b((?:sk|rk)_live_[0-9A-Za-z]{24,})")),
    ("wireguard key", "high", re.compile(
        r"PrivateKey\s*=\s*([A-Za-z0-9+/]{42,43}=)")),
    ("jwt", "medium", re.compile(
        r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,})")),
    ("url credentials", "medium", re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@\"'\\]+:([^/\s@\"'\\]{6,})@")),
    ("password assignment", "low", re.compile(
        r"(?i)\b(?:password|passwd|passphrase|secret|api[_-]?key|"
        r"access[_-]?token|auth[_-]?token|client[_-]?secret)"
        r"(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?"
        r"([^\s\"'\\,;{}()<>\[\]]{8,})")),
]

# Categories whose matches are often code or examples, not secrets
PLACEHOLDER_CHECKED = ("password assignment", "url credentials")
PLACEHOLDER_HINTS = ("xxx", "***", "...", "example", "changeme", "your",
                     "placeholder", "secret", "password", "redacted",
                     "dummy", "<", "$", "{", "%")
RE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
RE_SNAKE = re.compile(r"^[a-z_]+$|^[A-Z_]+$")

# (category, severity, regex) for Bash commands; the first match counts
COMMAND_PATTERNS = [
    ("pipe to shell", "high", re.compile(
        r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")),
    ("force push", "high", re.compile(
        r"\bgit\s+push\b[^;&|]*(?:--force(?![-\w])|\s-f\b)")),
    ("recursive delete", "high", re.compile(
        r"\brm\s+-[A-Za-z]*(?:rf|fr)[A-Za-z]*\s+\"?"
        r"(?:/|~/?|\*|\$HOME/?|/Users/\w+/?)\"?(?=$|[\s;&|])")),
    ("force push (lease)", "medium", re.compile(
        r"\bgit\s+push\b[^;&|]*--force-with-lease")),
    ("database drop", "medium", re.compile(
        r"(?i)\b(?:drop\s+(?:database|table|schema)|dropdb)\b")),
    ("disk tools", "medium", re.compile(
        r"(?:^|[;&|(]\s*|\s)(?:mkfs(?:\.\w+)?|dd\s+if=)")),
    ("sudo", "medium", re.compile(r"(?:^|[;&|]\s*)sudo\b")),
    ("keychain read", "medium", re.compile(
        r"\bsecurity\s+find-(?:generic|internet)-password\b")),
    ("hard reset", "medium", re.compile(
        r"\bgit\s+(?:reset\s+--hard|clean\s+-[A-Za-z]*f)")),
    ("world writable", "medium", re.compile(r"\bchmod\s+(?:-R\s+)?777\b")),
    ("delete", "low", re.compile(r"\brm\s+-[A-Za-z]*r")),
    ("remote shell", "low", re.compile(r"\b(?:ssh|scp|rsync)\s")),
    ("docker prune", "low", re.compile(r"\bdocker\s+\w*\s*prune\b")),
]

RE_HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
RE_SENSITIVE_PATH = re.compile(
    r"(?:/\.ssh/|/\.aws/|/\.gnupg/|/\.netrc|\.env(?:\.|$)|\.pem$|"
    r"\.kdbx$|id_(?:rsa|ed25519|ecdsa)|/\.config/gh/|credentials)")
RE_OUTWARD_TOOL = re.compile(
    r"(?i)(?:send|reply|forward|publish|deploy|delete|trash|share|"
    r"execute|transition|create_event|update_event)")

FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_COLOR = {"high": "31", "medium": "33", "low": "2"}
DETAIL_LIMIT = 160


# Audit: secrets

def looks_like_placeholder(value):
    """Return True for values that are code or example text."""
    lowered = value.lower()
    if any(hint in lowered for hint in PLACEHOLDER_HINTS):
        return True
    if RE_IDENTIFIER.match(value) or RE_SNAKE.match(value):
        return True
    return len(set(value)) < 4


def mask(value):
    """Return a masked form that still allows recognizing a secret."""
    if len(value) <= 8:
        return f"{value[:1]}\u2026({len(value)})"
    return f"{value[:4]}\u2026{value[-2:]} ({len(value)})"


def fingerprint(value):
    """Return a short hash that identifies a value without storing it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def secret_finding(category, severity, match):
    """Return a finding for one regex match, or None for noise."""
    value = match.group(1) if match.groups() else match.group(0)
    if category in PLACEHOLDER_CHECKED and looks_like_placeholder(value):
        return None
    shown = "BEGIN PRIVATE KEY" if category == "private key" \
        else mask(value)
    return Finding("secret", category, severity, shown, fingerprint(value))


def scan_secrets(line):
    """Yield secret findings in one raw transcript line."""
    for category, severity, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(line):
            finding = secret_finding(category, severity, match)
            if finding:
                yield finding


def record_origin(record):
    """Return where in the conversation a record sits."""
    if record.get("type") == "assistant":
        return "assistant"
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content):
        return "tool output"
    return "prompt" if record.get("type") == "user" else record.get("type")


# Audit: actions

def tool_uses(record):
    """Yield (name, input) of the tool calls in an assistant record."""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name") or "", block.get("input") or {}


def action_finding(category, severity, detail):
    """Return an action finding with a one line detail."""
    detail = " ".join(str(detail).split())[:DETAIL_LIMIT]
    return Finding("action", category, severity, detail,
                   fingerprint(category + detail))


def strip_heredocs(command):
    """Remove heredoc bodies: they are file content, not commands."""
    return RE_HEREDOC.sub("<<heredoc", command)


def excerpt(text, match):
    """Return the matched part of text with a little context."""
    start = max(match.start() - 30, 0)
    prefix = "\u2026" if start else ""
    return prefix + text[start: match.end() + 90]


def scan_command(command):
    """Yield the finding of one Bash command, if any."""
    executed = strip_heredocs(command)
    for category, severity, pattern in COMMAND_PATTERNS:
        match = pattern.search(executed)
        if match:
            yield action_finding(category, severity,
                                 excerpt(executed, match))
            return


def scan_tool_use(name, tool_input):
    """Yield findings for one tool call."""
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        yield from scan_command(command)
        if tool_input.get("dangerouslyDisableSandbox"):
            yield action_finding("sandbox disabled", "medium", command)
    elif name in FILE_TOOLS:
        path = str(tool_input.get("file_path", ""))
        if RE_SENSITIVE_PATH.search(path):
            yield action_finding("sensitive file " + name.lower(),
                                 "medium", short_path(path))
    elif name.startswith("mcp__") and RE_OUTWARD_TOOL.search(name):
        yield action_finding("outward tool", "low", name)


def scan_actions(record):
    """Yield action findings of one assistant record."""
    for name, tool_input in tool_uses(record):
        yield from scan_tool_use(name, tool_input)


# Audit: transcripts

def is_scannable(line):
    """Skip records that only duplicate the conversation."""
    return not line.startswith(('{"type":"api-request',
                                '{"type":"file-history'))


def permission_mode(line):
    """Return the mode of a permission-mode record, or None."""
    if not line.startswith('{"type":"permission-mode"'):
        return None
    record = parse_record(line)
    return record.get("permissionMode") if record else None


def audit_line(line):
    """Return the findings of one raw transcript line."""
    findings = list(scan_secrets(line))
    is_action = '"tool_use"' in line and '"type":"assistant"' in line
    if not findings and not is_action:
        return []
    record = parse_record(line) or {}
    if is_action:
        findings.extend(scan_actions(record))
    place = record_origin(record)
    for finding in findings:
        finding.where = place
        finding.time = record.get("timestamp")
    return findings


def merge_finding(found, finding):
    """Count a repeated finding instead of listing it twice."""
    if finding.key in found:
        found[finding.key].count += 1
    else:
        found[finding.key] = finding


def audit_transcript(path):
    """Return (findings, permission modes) of one transcript."""
    found = {}
    modes = set()
    with open(path, errors="replace") as handle:
        for line in handle:
            if not is_scannable(line):
                continue
            mode = permission_mode(line)
            if mode:
                modes.add(mode)
                continue
            for finding in audit_line(line):
                merge_finding(found, finding)
    if "bypassPermissions" in modes:
        merge_finding(found, action_finding(
            "permissions bypassed", "high", "bypassPermissions mode"))
    return list(found.values()), sorted(modes)


def audit_data(path):
    """Return the cacheable audit result of a transcript.

    Top level and returning plain data, so a process pool can run it.
    """
    findings, modes = audit_transcript(path)
    return {"findings": [dataclasses.asdict(f) for f in findings],
            "modes": modes}


def stale_transcripts(sessions, cache):
    """Return (path, key) of transcripts without a fresh audit."""
    stale = []
    for session in sessions:
        key = file_key(session.path)
        if cache.get(session.path, key) is None:
            stale.append((session.path, key))
    return stale


def scan_results(paths):
    """Yield audit data for paths, in parallel when there are many."""
    if len(paths) < PARALLEL_THRESHOLD:
        yield from map(audit_data, paths)
        return
    with concurrent.futures.ProcessPoolExecutor() as pool:
        yield from pool.map(audit_data, paths)


def refresh_audit_cache(sessions, cache):
    """Scan every transcript whose audit result is missing or stale."""
    stale = stale_transcripts(sessions, cache)
    progress = Progress("scanning", len(stale))
    results = scan_results([path for path, _ in stale])
    for done, ((path, key), data) in enumerate(zip(stale, results), 1):
        cache.put(path, key, data)
        progress.update(done)
    progress.finish()


# Audit: report

def wanted(finding, args):
    """Return True when a finding passes --only and --min-severity."""
    if args.only and finding.group != args.only:
        return False
    rank = SEVERITY_ORDER[finding.severity]
    return rank <= SEVERITY_ORDER[args.min_severity]


def finding_order(finding):
    """Order findings by severity, then group and category."""
    return (SEVERITY_ORDER[finding.severity], finding.group,
            finding.category)


def format_finding(finding):
    """Return one indented report line for a finding."""
    count = f"x{finding.count}" if finding.count > 1 else ""
    return "    " + " ".join([
        paint(finding.severity.ljust(6), SEVERITY_COLOR[finding.severity]),
        clip(finding.category, 20),
        dim(clip(finding.where, 11)),
        count.rjust(4),
        finding.detail,
    ])


def print_session_audit(session, modes, findings, layout):
    """Print the report block of one session."""
    print(format_row(session, layout))
    if modes:
        print("    " + dim("modes  " + ", ".join(modes)))
    for finding in findings:
        print(format_finding(finding))
    print()


def print_audit_summary(findings, session_count):
    """Print totals per severity and the number of distinct secrets."""
    totals = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        totals[finding.severity] += 1
    secrets = {f.fp for f in findings if f.group == "secret"}
    print(f"{session_count} sessions scanned: {totals['high']} high, "
          f"{totals['medium']} medium, {totals['low']} low, "
          f"{len(secrets)} distinct secrets")


def session_findings(session, cache, args):
    """Return (modes, wanted findings) of a session from the cache."""
    data = cache.get(session.path, file_key(session.path)) or {}
    findings = [Finding(**f) for f in data.get("findings", [])]
    selected = sorted((f for f in findings if wanted(f, args)),
                      key=finding_order)
    return data.get("modes", []), selected


def cmd_audit(args, sessions):
    """Scan transcripts for secrets and risky actions."""
    # Superseded and empty transcripts are on disk all the same
    filters = Filters.from_args(args, include_hidden=True)
    selected = select_sessions(sessions, filters)
    cache = TranscriptCache(AUDIT_CACHE, AUDIT_VERSION)
    refresh_audit_cache(selected, cache)
    cache.save()
    layout = table_layout()
    shown = []
    for session in selected:
        modes, findings = session_findings(session, cache, args)
        if findings:
            print_session_audit(session, modes, findings[: args.max],
                                layout)
        shown.extend(findings)
    print_audit_summary(shown, len(selected))


# Memory
#
# Claude Code keeps auto memories per project as Markdown files with a
# small frontmatter, listed in an index file MEMORY.md that is loaded
# into every session of that project. Instructions that apply
# everywhere belong in ~/.claude/CLAUDE.md, which every session loads.

@dataclass
class Memory:
    """One memory file of a project, or of ~/.claude/memory."""

    path: str
    project: str
    name: str = ""
    description: str = ""
    type: str = ""
    body: str = ""
    indexed: bool = True
    loaded: bool = True

    @property
    def stem(self):
        return os.path.basename(self.path)[: -len(".md")]

    @property
    def label(self):
        """Project and file name, unique across all memories."""
        return f"{short_path(self.project)}/{self.stem}"


MEMORY_INDEX = "MEMORY.md"
FRONTMATTER = "---\n"


def split_frontmatter(text):
    """Return (frontmatter lines, body) of a Markdown file."""
    if not text.startswith(FRONTMATTER):
        return [], text
    head, separator, body = text[len(FRONTMATTER):].partition("\n---")
    if not separator:
        return [], text
    return head.splitlines(), body.lstrip("-").lstrip("\n")


def frontmatter_fields(lines):
    """Return the key: value pairs of frontmatter lines.

    Nested keys (metadata: / type: ...) are flattened, which is all
    the memory format needs.
    """
    fields = {}
    for line in lines:
        key, colon, value = line.strip().partition(":")
        if colon and value.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def index_links(directory):
    """Return the file names linked from a memory index."""
    text = read_text(os.path.join(directory, MEMORY_INDEX))
    return set(re.findall(r"\]\(([^)]+\.md)\)", text))


def read_text(path):
    """Return the text of a file, or "" if it cannot be read."""
    try:
        with open(path, errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def read_memory(path, project, linked, loaded=True):
    """Parse one memory file."""
    lines, body = split_frontmatter(read_text(path))
    fields = frontmatter_fields(lines)
    stem = os.path.basename(path)[: -len(".md")]
    return Memory(path=path, project=project,
                  name=fields.get("name", stem),
                  description=fields.get("description", ""),
                  type=fields.get("type", ""), body=body.strip(),
                  indexed=os.path.basename(path) in linked,
                  loaded=loaded)


def memory_directories(sessions):
    """Yield (memory directory, project directory, loaded) triples.

    The project directory comes from the sessions of that project
    folder; the folder name itself cannot be decoded reliably.
    """
    projects = {os.path.basename(os.path.dirname(s.path)): s.cwd
                for s in sessions}
    for directory in sorted(glob_memory_dirs()):
        slug = os.path.basename(os.path.dirname(directory))
        yield directory, projects.get(slug) or decode_slug(slug), True
    if os.path.isdir(USER_MEMORY):
        yield USER_MEMORY, USER_MEMORY, False


def glob_memory_dirs():
    """Return the memory directories of all project folders."""
    if not os.path.isdir(PROJECTS):
        return []
    found = (os.path.join(PROJECTS, slug, "memory")
             for slug in os.listdir(PROJECTS))
    return [path for path in found if os.path.isdir(path)]


def load_memories(sessions):
    """Return every memory, grouped by project."""
    memories = []
    for directory, project, loaded in memory_directories(sessions):
        linked = index_links(directory)
        for name in sorted(os.listdir(directory)):
            if name.endswith(".md") and name != MEMORY_INDEX:
                path = os.path.join(directory, name)
                memories.append(read_memory(path, project, linked, loaded))
    return memories


def memory_matches(memory, terms):
    """Return True when every term occurs in the memory."""
    text = " ".join([memory.label, memory.name, memory.description,
                     memory.type, memory.body]).lower()
    return all(term.lower() in text for term in terms)


def select_memories(memories, args):
    """Return the memories passing --type, --under/--here and query."""
    under = os.getcwd() if args.here else full_path(args.under)
    return [m for m in memories
            if (not args.type or m.type == args.type)
            and (under is None or m.project == under
                 or m.project.startswith(under + os.sep))
            and memory_matches(m, args.query)]


def find_memories(memories, ref):
    """Return the memories a reference could mean.

    A reference is a file name without .md, a name from the
    frontmatter, or project/file as printed in the list.
    """
    ref = ref.lower().removesuffix(".md")
    exact = [m for m in memories
             if ref in (m.stem.lower(), m.name.lower())]
    if exact:
        return exact
    return [m for m in memories if ref in m.label.lower()]


def resolve_memory(memories, ref):
    """Return the one memory ref means, or exit naming candidates."""
    found = find_memories(memories, ref)
    if len(found) == 1:
        return found[0]
    if not found:
        fail(f"no memory matches {ref!r}")
    print(f"claudes: {ref!r} matches {len(found)} memories:",
          file=sys.stderr)
    for memory in found[:20]:
        print("  " + memory.label, file=sys.stderr)
    print("Use project/name as printed above.", file=sys.stderr)
    sys.exit(1)


def memory_flags(memory):
    """Return markers for memories that are unused or orphaned."""
    if not memory.loaded:
        return paint("not loaded", "31")
    if not os.path.isdir(memory.project):
        return paint("project gone", "31")
    if not memory.indexed:
        return paint("not in index", "33")
    return ""


def format_memory(memory, width):
    """Return one list line for a memory."""
    return GAP.join([
        paint(clip(memory.type or "-", 9), "35"),
        paint(clip_left(memory.label, 44), "36"),
        clip(memory.description or memory.name, width),
        memory_flags(memory),
    ]).rstrip()


def cmd_memory_list(args, memories):
    """Print one line per memory."""
    selected = select_memories(memories, args)
    width = max(table_layout().title + table_layout().directory - 44, 30)
    for memory in selected:
        print(format_memory(memory, width))
    if sys.stdout.isatty():
        projects = len({m.project for m in selected})
        print(dim(f"{len(selected)} memories in {projects} projects"))


def cmd_memory_show(args, memories):
    """Print a memory with its metadata."""
    memory = resolve_memory(memories, args.ref)
    rows = [("name", memory.name), ("type", memory.type),
            ("project", short_path(memory.project)),
            ("file", short_path(memory.path)),
            ("note", memory_flags(memory))]
    for label, value in rows:
        if value:
            print(f"{dim(label.ljust(10))} {value}")
    print("\n" + (memory.description and memory.description + "\n\n")
          + memory.body)


def cmd_memory_grep(args, memories):
    """Print matching lines of the memories containing every term."""
    terms = [t.lower() for t in args.text]
    for memory in memories:
        if not memory_matches(memory, terms):
            continue
        print(paint(memory.label, "36"))
        for line in memory.body.splitlines():
            if any(t in line.lower() for t in terms):
                print("    " + highlight(line.strip(), terms[0]))


def duplicate_groups(memories):
    """Return lists of memories sharing a name across projects."""
    groups = {}
    for memory in memories:
        groups.setdefault(memory.name.lower(), []).append(memory)
    return [group for group in groups.values()
            if len({m.project for m in group}) > 1]


def cmd_memory_dupes(args, memories):
    """List memories kept in several projects: global candidates."""
    groups = duplicate_groups(memories)
    for group in groups:
        print(paint(group[0].name, "1"))
        for memory in group:
            print("    " + paint(memory.label, "36") + "  "
                  + dim(clip(memory.description, 60).rstrip()))
    if not groups:
        print("no memory appears in more than one project")


def claude_md_section(memory):
    """Return the Markdown section promote appends to CLAUDE.md."""
    title = memory.name or memory.stem
    lines = [f"## {title}", ""]
    if memory.description:
        lines += [memory.description, ""]
    lines.append(memory.body)
    return "\n".join(lines).rstrip() + "\n"


def section_exists(text, memory):
    """Return True when CLAUDE.md has a heading with the memory name."""
    pattern = r"^#+\s+" + re.escape(memory.name or memory.stem) + r"\s*$"
    return re.search(pattern, text, re.MULTILINE | re.IGNORECASE)


def blank_line_before(text):
    """Return what to append so that exactly one blank line follows."""
    if not text or text.endswith("\n\n"):
        return ""
    return "\n" if text.endswith("\n") else "\n\n"


def append_to_claude_md(sections):
    """Back up CLAUDE.md, then append the sections to it."""
    current = read_text(GLOBAL_CLAUDE_MD)
    if current:
        shutil.copy2(GLOBAL_CLAUDE_MD, GLOBAL_CLAUDE_MD + ".claudes-bak")
    with open(GLOBAL_CLAUDE_MD, "a") as handle:
        handle.write(blank_line_before(current) + "\n".join(sections))


def remove_index_line(memory):
    """Drop the line that links a memory from its MEMORY.md."""
    index = os.path.join(os.path.dirname(memory.path), MEMORY_INDEX)
    text = read_text(index)
    link = f"({os.path.basename(memory.path)})"
    kept = [line for line in text.splitlines(keepends=True)
            if link not in line]
    if len(kept) != len(text.splitlines(keepends=True)):
        with open(index, "w") as handle:
            handle.writelines(kept)


def trash_memory(memory):
    """Move a memory file to the Trash and unlink it from the index."""
    slug = os.path.basename(os.path.dirname(os.path.dirname(memory.path)))
    target = os.path.join(TRASH, "memory", slug or "user")
    os.makedirs(target, exist_ok=True)
    shutil.move(memory.path, os.path.join(target,
                                          os.path.basename(memory.path)))
    remove_index_line(memory)


def cmd_memory_promote(args, memories):
    """Append memories to ~/.claude/CLAUDE.md, optionally moving them."""
    chosen = [resolve_memory(memories, ref) for ref in args.refs]
    current = read_text(GLOBAL_CLAUDE_MD)
    for memory in chosen:
        if section_exists(current, memory):
            fail(f"{short_path(GLOBAL_CLAUDE_MD)} already has a section "
                 f"{memory.name!r}")
    sections = [claude_md_section(m) for m in chosen]
    print(dim(f"appending to {short_path(GLOBAL_CLAUDE_MD)}:") + "\n")
    print("\n".join(sections))
    action = "Append" + (" and move the memories to the Trash"
                         if args.move else "")
    if not args.yes and not confirm(action + "?"):
        return
    append_to_claude_md(sections)
    for memory in chosen if args.move else []:
        trash_memory(memory)
    print(f"appended {len(chosen)} section(s); backup in "
          f"{short_path(GLOBAL_CLAUDE_MD)}.claudes-bak")


def cmd_memory_rm(args, memories):
    """Move memories to the Trash and drop them from their index."""
    chosen = [resolve_memory(memories, ref) for ref in args.refs]
    for memory in chosen:
        print(paint(memory.label, "36") + "  " + dim(memory.description))
    if not args.yes and not confirm(f"Move {len(chosen)} to the Trash?"):
        return
    for memory in chosen:
        trash_memory(memory)
    print(f"moved {len(chosen)} memory file(s) to {short_path(TRASH)}")


def cmd_memory_edit(args, memories):
    """Open a memory in $EDITOR."""
    memory = resolve_memory(memories, args.ref)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    os.execvp("sh", ["sh", "-c", f'{editor} "$0"', memory.path])


def cmd_memory(args, sessions):
    """Dispatch a memory subcommand."""
    args.memory_func(args, load_memories(sessions))


# Command line

def filter_options():
    """Return a parent parser with the shared filter options."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("filters")
    group.add_argument("-a", "--all", action="store_true",
                       help="include empty and continued sessions")
    group.add_argument("--dir", metavar="PATH",
                       help="only sessions started exactly in PATH")
    group.add_argument("--under", metavar="PATH",
                       help="only sessions started in or below PATH")
    group.add_argument("--here", action="store_true",
                       help="same as --under with the current directory")
    group.add_argument("--older", type=parse_duration, metavar="AGE",
                       help="idle for at least AGE: 30m, 12h, 30d, 2w")
    group.add_argument("--newer", type=parse_duration, metavar="AGE",
                       help="active within the last AGE")
    group.add_argument("--from", dest="ids_from", metavar="FILE",
                       help="only session ids listed in FILE, - for stdin")
    named = group.add_mutually_exclusive_group()
    named.add_argument("--named", dest="named", action="store_true",
                       default=None, help="only sessions you named")
    named.add_argument("--unnamed", dest="named", action="store_false",
                       help="only sessions without a name of yours")
    origin = group.add_mutually_exclusive_group()
    origin.add_argument("--app", dest="origin", action="store_const",
                        const="app", help="started in the Claude app")
    origin.add_argument("--term", dest="origin", action="store_const",
                        const="term", help="started in a terminal")
    group.add_argument("-s", "--sort", choices=list(SORT_KEYS),
                       default="active", help="sort key (default: active)")
    group.add_argument("-R", "--reverse", action="store_true",
                       help="reverse the sort order")
    group.add_argument("-n", "--limit", type=int, default=0,
                       metavar="N", help="show at most N sessions")
    return parent


def query_argument(parser):
    """Add the free text query that all terms must match."""
    parser.add_argument("query", nargs="*",
                        help="words to look for in title, directory, id,"
                             " branch and prompts")


def add_browse_commands(sub, filters):
    """Add list, pick, show and resume."""
    listing = sub.add_parser("list", aliases=["ls"], parents=[filters],
                             help="list sessions (default command)")
    query_argument(listing)
    output = listing.add_mutually_exclusive_group()
    output.add_argument("-q", "--ids", action="store_true",
                        help="print only session ids, for pipes")
    output.add_argument("--format", metavar="TEMPLATE",
                        help="print each session as TEMPLATE, such as"
                             " '{short_id} {directory}'")
    output.add_argument("--json", action="store_true",
                        help="print JSON instead of a table")
    listing.set_defaults(func=cmd_list)

    pick = sub.add_parser("pick", aliases=["p"], parents=[filters],
                          help="choose a session with fzf and resume it")
    query_argument(pick)
    pick.set_defaults(func=cmd_pick)

    show = sub.add_parser("show", help="details of sessions")
    show.add_argument("refs", metavar="ID", nargs="+",
                      help="- reads ids from stdin")
    show.set_defaults(func=cmd_show)

    resume = sub.add_parser("resume", aliases=["r"],
                            help="resume a session in its directory")
    resume.add_argument("ref", metavar="ID", nargs="?",
                        help="omit to pick one with fzf")
    resume.add_argument("--fork", action="store_true",
                        help="branch off into a new session id")
    resume.set_defaults(func=cmd_resume)


def add_manage_commands(sub, filters):
    """Add rename, rm and grep."""
    rename = sub.add_parser("rename", help="set the session title")
    rename.add_argument("ref", metavar="ID")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_rename)

    remove = sub.add_parser("rm", parents=[filters],
                            help="move sessions to the Trash")
    remove.add_argument("refs", metavar="ID", nargs="*",
                        help="- reads ids from stdin")
    remove.add_argument("--empty", action="store_true",
                        help="also every session without a prompt")
    remove.add_argument("-y", "--yes", action="store_true",
                        help="do not ask for confirmation")
    remove.set_defaults(func=cmd_rm)

    grep = sub.add_parser("grep", parents=[filters],
                          help="search the text of all conversations")
    grep.add_argument("text", nargs="+")
    grep.add_argument("--max", type=int, default=3, metavar="N",
                      help="snippets per session (default: 3)")
    grep.set_defaults(func=cmd_grep)


def add_security_commands(sub, filters):
    """Add audit and review."""
    audit = sub.add_parser("audit", parents=[filters],
                           help="find secrets and risky actions")
    query_argument(audit)
    audit.add_argument("--only", choices=["secret", "action"])
    audit.add_argument("--min-severity", choices=list(SEVERITY_ORDER),
                       default="medium",
                       help="lowest severity shown (default: medium)")
    audit.add_argument("--max", type=int, default=15, metavar="N",
                       help="findings shown per session (default: 15)")
    audit.set_defaults(func=cmd_audit)

    review = sub.add_parser("review",
                            help="run /security-review in a repository")
    review.add_argument("ref", metavar="ID|DIR",
                        help="session id, title or directory")
    review.add_argument("focus", nargs="*",
                        help="extra instructions for the review")
    review.set_defaults(func=cmd_review)


def add_memory_commands(sub):
    """Add memory with its own subcommands; list is the default."""
    memory = sub.add_parser("memory", aliases=["mem"],
                            help="list, read and promote auto memories")
    memory.set_defaults(func=cmd_memory)
    actions = memory.add_subparsers(dest="memory_command",
                                    metavar="ACTION")

    listing = actions.add_parser("list", aliases=["ls"],
                                 help="list memories (default)")
    listing.add_argument("query", nargs="*")
    listing.add_argument("--type", help="feedback, project, reference,"
                                        " user")
    listing.add_argument("--under", metavar="PATH",
                         help="only projects in or below PATH")
    listing.add_argument("--here", action="store_true",
                         help="only this directory and below")
    listing.set_defaults(memory_func=cmd_memory_list)

    show = actions.add_parser("show", help="print one memory")
    show.add_argument("ref", metavar="MEMORY")
    show.set_defaults(memory_func=cmd_memory_show)

    grep = actions.add_parser("grep", help="search all memories")
    grep.add_argument("text", nargs="+")
    grep.set_defaults(memory_func=cmd_memory_grep)

    dupes = actions.add_parser("dupes", help="memories kept in several"
                                             " projects")
    dupes.set_defaults(memory_func=cmd_memory_dupes)

    promote = actions.add_parser(
        "promote", help="append memories to ~/.claude/CLAUDE.md")
    promote.add_argument("refs", metavar="MEMORY", nargs="+")
    promote.add_argument("--move", action="store_true",
                         help="then move the memory files to the Trash")
    promote.add_argument("-y", "--yes", action="store_true")
    promote.set_defaults(memory_func=cmd_memory_promote)

    remove = actions.add_parser("rm", help="move memories to the Trash")
    remove.add_argument("refs", metavar="MEMORY", nargs="+")
    remove.add_argument("-y", "--yes", action="store_true")
    remove.set_defaults(memory_func=cmd_memory_rm)

    edit = actions.add_parser("edit", help="open a memory in $EDITOR")
    edit.add_argument("ref", metavar="MEMORY")
    edit.set_defaults(memory_func=cmd_memory_edit)
    return set(actions.choices)


EXAMPLES = """\
examples:
  claudes                      sessions, most recently active first
  claudes api --term           terminal sessions matching "api"
  claudes --unnamed -s msgs    unnamed sessions, longest first
  claudes -r api-refactor      resume in its own directory
  claudes pick                 fuzzy finder, Enter resumes
  claudes audit                secrets and risky actions

selecting many sessions:
  claudes --unnamed --dir ~ --older 30d       what would match
  claudes rm --unnamed --dir ~ --older 30d    move them to the Trash
  claudes --older 60d -q | claudes rm -       the same through a pipe
  claudes --app -q | claudes audit --from -   audit only those

memories:
  claudes memory                         every memory of every project
  claudes memory dupes                   kept in several projects
  claudes memory promote no_dashes       append to ~/.claude/CLAUDE.md
"""


def build_parser():
    """Return the parser and the names that select a subcommand."""
    parser = argparse.ArgumentParser(
        prog="claudes", epilog=EXAMPLES,
        description="List, find, resume and audit Claude Code sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    filters = filter_options()
    add_browse_commands(sub, filters)
    add_manage_commands(sub, filters)
    add_security_commands(sub, filters)
    memory_actions = add_memory_commands(sub)
    commands = set(sub.choices) | {"-h", "--help", "--version"}
    return parser, commands, memory_actions


# "claudes -r ID" resumes, the habit from "claude -r ID"
RESUME_FLAGS = {"-r", "--resume"}


MEMORY_COMMANDS = {"memory", "mem"}
HELP_FLAGS = {"-h", "--help"}


def normalize_argv(argv, commands, memory_actions):
    """Map -r to resume and make list the default, also for memory."""
    if argv and argv[0] in RESUME_FLAGS:
        return ["resume"] + argv[1:]
    if not argv or argv[0] not in commands:
        return ["list"] + argv
    if argv[0] in MEMORY_COMMANDS:
        rest = argv[1:]
        if not rest or rest[0] not in memory_actions | HELP_FLAGS:
            return [argv[0], "list"] + rest
    return argv


def main(argv=None):
    """Parse the command line and run the command."""
    parser, commands, memory_actions = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(normalize_argv(argv, commands, memory_actions))
    args.func(args, load_sessions())


def run():
    """Console entry point: quiet on ctrl-c and on a closed pipe."""
    try:
        main()
        # Flush here, where a closed pipe can still be caught
        sys.stdout.flush()
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Python would complain again when flushing stdout at exit
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)


if __name__ == "__main__":
    run()
