#!/usr/bin/env python3
"""List, find, resume and manage Claude Code sessions.

Reads the transcripts under ~/.claude/projects and the live session
registry under ~/.claude/sessions. Metadata is cached per transcript
(keyed by mtime and size) in ~/.cache/claude-sessions.json, so only
changed transcripts are parsed again.

Usage:
  claudes [list] [QUERY...] [FILTERS]   list sessions
  claudes pick [QUERY...] [FILTERS]     fzf picker, Enter resumes
  claudes show ID                       details of one session
  claudes resume ID [--fork]            resume in its directory
  claudes rename ID NAME                set the session title
  claudes rm ID...                      move sessions to Trash
  claudes grep TEXT                     full text search
  claudes audit [QUERY...] [FILTERS]    secrets and risky actions
  claudes review ID|DIR [FOCUS...]      start /security-review there

Filters:
  -a, --all              include empty and continued sessions
  --here                 only sessions in or below the current directory
  --named, --unnamed     sessions with or without a title you gave
  --app, --term          started in the Claude app or in a terminal
  -s, --sort KEY         active (default), started, name, dir, msgs,
                         size, cost
  -r, --reverse          reverse the sort order
  -n, --limit N          show at most N sessions

ID is a session id prefix or (part of) a session title.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

__version__ = "0.1.0"

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE, "projects")
REGISTRY = os.path.join(CLAUDE, "sessions")
CACHE = os.path.join(HOME, ".cache", "claude-sessions.json")
TRASH = os.path.join(HOME, ".Trash", "claude-sessions")
CACHE_VERSION = 3

RE_TIMESTAMP = re.compile(r'"timestamp":"([^"]+)"')
RE_ENTRYPOINT = re.compile(r'"entrypoint":"([^"]+)"')
RE_KIND = re.compile(r'"sessionKind":"([^"]+)"')
RE_CWD = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')
RE_BRANCH = re.compile(r'"gitBranch":"((?:[^"\\]|\\.)*)"')
RE_COMMAND = re.compile(r"<command-name>([^<]+)</command-name>")
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$")

TITLE_TYPES = {
    "custom-title": ("customTitle", "user"),
    "agent-name": ("agentName", "agent"),
    "ai-title": ("aiTitle", "ai"),
}
NAMED_SOURCES = ("user", "agent")
ENTRYPOINT_LABELS = {"claude-desktop": "app", "cli": "term"}

# Sort key name -> (function on a session, descending by default)
SORT_KEYS = {
    "active": (lambda s: s["last"] or "", True),
    "started": (lambda s: s["first"] or "", True),
    "name": (lambda s: s["title"].lower(), False),
    "dir": (lambda s: (s["cwd"] or "").lower(), False),
    "msgs": (lambda s: s["prompts"], True),
    "size": (lambda s: s["size"], True),
    "cost": (lambda s: s["cost"] or 0, True),
}


# Output helpers

def use_color():
    """Return True when stdout is a terminal that wants color."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text, code):
    """Wrap text in an ANSI color code when color is enabled."""
    if not use_color():
        return text
    return "\033[{}m{}\033[0m".format(code, text)


def clip(text, width):
    """Cut text to width characters, marking the cut with an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "…"


def short_path(path):
    """Replace the home directory prefix with a tilde."""
    if path and (path == HOME or path.startswith(HOME + os.sep)):
        return "~" + path[len(HOME):]
    return path or "?"


def fail(message):
    """Print an error message and exit with status 1."""
    print("claudes: " + message, file=sys.stderr)
    sys.exit(1)


# Time helpers

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
    seconds = (now - stamp).total_seconds()
    if seconds < 3600:
        return "{}m".format(max(int(seconds // 60), 0))
    if seconds < 86400:
        return "{}h".format(int(seconds // 3600))
    if seconds < 86400 * 14:
        return "{}d".format(int(seconds // 86400))
    return stamp.astimezone().strftime("%d.%m.%y")


def format_local(value):
    """Return a timestamp as local date and time."""
    stamp = parse_iso(value)
    if stamp is None:
        return "?"
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


# Transcript parsing

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


def prompt_text(record):
    """Return the human prompt of a user record, or None."""
    if record.get("isMeta") or record.get("isCompactSummary"):
        return None
    message = record.get("message") or {}
    text = message_text(message.get("content")).strip()
    command = RE_COMMAND.search(text)
    if command:
        return command.group(1).strip()
    if not text or text.startswith("<"):
        return None
    return text


def empty_meta():
    """Return a metadata dict with all fields unset."""
    return {
        "cwd": None, "branch": None, "first": None, "last": None,
        "titles": {}, "first_prompt": None, "last_prompt": None,
        "prompts": 0, "continued_in": None, "cost": None, "prs": [],
        "entrypoints": [], "kind": None,
    }


def apply_typed_record(meta, record):
    """Merge a title, link or cost record into the metadata."""
    kind = record.get("type")
    if kind in TITLE_TYPES:
        key, source = TITLE_TYPES[kind]
        if record.get(key):
            meta["titles"][source] = record[key]
    elif kind == "continued-in":
        meta["continued_in"] = record.get("continuedInSessionId")
    elif kind == "cost-state":
        meta["cost"] = record.get("totalCostUSD")
    elif kind == "pr-link" and record.get("prUrl"):
        if record["prUrl"] not in meta["prs"]:
            meta["prs"].append(record["prUrl"])


def apply_user_record(meta, record):
    """Count a human prompt and remember the first and last one."""
    text = prompt_text(record)
    if text is None:
        return
    meta["prompts"] += 1
    meta["first_prompt"] = meta["first_prompt"] or text[:500]
    meta["last_prompt"] = text[:500]


def apply_context(meta, line):
    """Pick up timestamp, cwd and branch from a raw line."""
    stamp = RE_TIMESTAMP.search(line)
    if stamp:
        value = stamp.group(1)
        if meta["first"] is None or value < meta["first"]:
            meta["first"] = value
        if meta["last"] is None or value > meta["last"]:
            meta["last"] = value
    if meta["cwd"] is None:
        cwd = RE_CWD.search(line)
        if cwd:
            meta["cwd"] = json.loads('"' + cwd.group(1) + '"')
    branch = RE_BRANCH.search(line)
    if branch:
        meta["branch"] = branch.group(1)
    entrypoint = RE_ENTRYPOINT.search(line)
    if entrypoint and entrypoint.group(1) not in meta["entrypoints"]:
        meta["entrypoints"].append(entrypoint.group(1))
    kind = RE_KIND.search(line)
    if kind:
        meta["kind"] = kind.group(1)


def is_typed_line(line):
    """Return True for small records that carry session metadata."""
    return line.startswith('{"type":"') and not line.startswith(
        ('{"type":"api-request', '{"type":"file-history',
         '{"type":"attachment"'))


def is_prompt_line(line):
    """Return True for user lines that may hold a human prompt."""
    return '"type":"user"' in line and '"tool_result"' not in line


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
        apply_context(meta, line)


def read_meta(path):
    """Parse one transcript and return its metadata."""
    meta = empty_meta()
    with open(path, errors="replace") as handle:
        for line in handle:
            apply_line(meta, line)
    return meta


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


def load_cache():
    """Return the cached metadata keyed by transcript path."""
    try:
        with open(CACHE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("entries", {})


def save_cache(entries):
    """Write the metadata cache atomically."""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    temp = CACHE + ".tmp"
    with open(temp, "w") as handle:
        json.dump({"version": CACHE_VERSION, "entries": entries}, handle)
    os.replace(temp, CACHE)


def file_key(path):
    """Return the cache validity key (mtime, size) of a file."""
    stat = os.stat(path)
    return [stat.st_mtime_ns, stat.st_size]


def cached_meta(path, cache):
    """Return metadata from the cache, parsing the file if stale."""
    key = file_key(path)
    entry = cache.get(path)
    if entry and entry.get("key") == key:
        return entry["meta"]
    meta = read_meta(path)
    cache[path] = {"key": key, "meta": meta}
    return meta


def pick_title(meta):
    """Return (title, source) with the best available title."""
    titles = meta["titles"]
    for source in ("user", "agent", "ai"):
        if titles.get(source):
            return titles[source], source
    if meta["first_prompt"]:
        return meta["first_prompt"], "prompt"
    return "", None


def decode_slug(slug):
    """Guess a directory from a project slug (lossy fallback)."""
    return slug.replace("-", "/")


def build_session(path, meta):
    """Combine path and metadata into a session dict."""
    title, source = pick_title(meta)
    slug = os.path.basename(os.path.dirname(path))
    session = dict(meta)
    session.update({
        "id": os.path.basename(path)[:-6],
        "path": path,
        "cwd": meta["cwd"] or decode_slug(slug),
        "title": title,
        "title_source": source,
        "size": os.path.getsize(path),
        "live": None,
    })
    return session


def load_sessions():
    """Return all sessions, newest activity first."""
    cache = load_cache()
    paths = list(iter_transcripts())
    sessions = [build_session(p, cached_meta(p, cache)) for p in paths]
    save_cache({p: cache[p] for p in paths if p in cache})
    live = load_live()
    for session in sessions:
        session["live"] = live.get(session["id"])
    sessions.sort(key=lambda s: s["last"] or "", reverse=True)
    return sessions


# Live sessions

def pid_alive(pid):
    """Return True when a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_live_entry(path):
    """Return a registry entry if its process still runs."""
    try:
        with open(path) as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    pid = entry.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return None
    return entry


def load_live():
    """Return running sessions from the registry keyed by id."""
    if not os.path.isdir(REGISTRY):
        return {}
    live = {}
    for entry in os.listdir(REGISTRY):
        if not entry.endswith(".json"):
            continue
        info = read_live_entry(os.path.join(REGISTRY, entry))
        if info and info.get("sessionId"):
            live[info["sessionId"]] = info
    return live


def process_tty(pid):
    """Return the controlling terminal of a process, or None."""
    result = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                            capture_output=True, text=True)
    tty = result.stdout.strip()
    return tty if tty and tty != "??" else None


def live_label(session):
    """Return a short status label for a running session."""
    live = session["live"]
    if not live:
        return ""
    kind = "bg" if live.get("kind") == "bg" else "run"
    return "{}:{}".format(kind, live.get("status") or "?")


# Origin

def entrypoints(session):
    """Return the entrypoints of a session, oldest first."""
    found = list(session["entrypoints"])
    live = session["live"] or {}
    if live.get("entrypoint") and live["entrypoint"] not in found:
        found.append(live["entrypoint"])
    return found


def origin(session):
    """Return where a session was started: app, term or other."""
    found = entrypoints(session)
    if not found:
        return "?"
    return ENTRYPOINT_LABELS.get(found[0], found[0])


def source_label(session):
    """Return the origin, followed by later origins such as app>term."""
    labels = [ENTRYPOINT_LABELS.get(e, e) for e in entrypoints(session)]
    label = ">".join(labels) or "?"
    if session["kind"] == "bg":
        label += " bg"
    return label


def is_named(session):
    """Return True when the session has a title you set."""
    return session["title_source"] in NAMED_SOURCES


# Filtering and lookup

def search_text(session):
    """Return the lowercased text that list queries match against."""
    fields = [session["title"], session["cwd"], session["id"],
              session["first_prompt"], session["branch"]]
    fields.extend(session["titles"].values())
    return " ".join(f for f in fields if f).lower()


def matches(session, terms):
    """Return True when every query term occurs in the session."""
    text = search_text(session)
    return all(term.lower() in text for term in terms)


def is_visible(session, show_all):
    """Hide empty and superseded sessions unless show_all is set."""
    if show_all or session["live"]:
        return True
    return session["prompts"] > 0 and not session["continued_in"]


def in_directory(session, directory):
    """Return True when the session ran in or below directory."""
    cwd = session["cwd"] or ""
    return cwd == directory or cwd.startswith(directory + os.sep)


def keep_session(session, args):
    """Return True when a session passes all list filters."""
    if not is_visible(session, args.all):
        return False
    if args.named is not None and is_named(session) != args.named:
        return False
    if args.origin and origin(session) != args.origin:
        return False
    if args.here and not in_directory(session, os.getcwd()):
        return False
    return matches(session, args.query)


def sort_sessions(sessions, key, reverse):
    """Return sessions ordered by a SORT_KEYS key."""
    function, descending = SORT_KEYS[key]
    return sorted(sessions, key=function, reverse=descending != reverse)


def select_sessions(args, sessions):
    """Return the filtered, sorted and limited sessions."""
    selected = [s for s in sessions if keep_session(s, args)]
    selected = sort_sessions(selected, args.sort, args.reverse)
    return selected[: args.limit] if args.limit else selected


def resolve(sessions, ref, newest=False):
    """Return the one session matching an id prefix or title.

    With newest set, a title shared by several sessions resolves to
    the most recently active one instead of being ambiguous.
    """
    ref_lower = ref.lower()
    by_id = [s for s in sessions if s["id"].startswith(ref_lower)]
    if len(by_id) == 1:
        return by_id[0]
    exact = [s for s in sessions if s["title"].lower() == ref_lower]
    if len(exact) == 1 or (exact and newest):
        return exact[0]
    fuzzy = by_id or exact or [
        s for s in sessions if ref_lower in s["title"].lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        fail("no session matches {!r}".format(ref))
    print("claudes: {!r} is ambiguous:".format(ref), file=sys.stderr)
    for session in fuzzy[:20]:
        print("  " + format_row(session), file=sys.stderr)
    sys.exit(1)


# Rendering

def title_cell(session, width):
    """Return the title column, dimmed when it is only a prompt."""
    text = clip(session["title"], width)
    if session["title_source"] == "user":
        return paint(text, "1")
    if session["title_source"] == "prompt":
        return paint(text, "2")
    return text


def format_row(session):
    """Return one list line for a session."""
    status = live_label(session)
    status_cell = paint(clip(status, 9), "32") if status else " " * 9
    return "  ".join([
        paint(session["id"][:8], "33"),
        format_age(session["last"]).rjust(8),
        status_cell,
        paint(clip(source_label(session), 8), "35"),
        title_cell(session, 36),
        paint(clip(short_path(session["cwd"]), 32), "36"),
        str(session["prompts"]).rjust(4),
    ])


def header_row():
    """Return the column header line."""
    return paint("  ".join([
        "id".ljust(8), "active".rjust(8), "status".ljust(9),
        "source".ljust(8), "title".ljust(36), "directory".ljust(32),
        "msgs",
    ]), "4")


def show_session(session):
    """Print every known detail of one session."""
    rows = [
        ("id", session["id"]),
        ("title", "{} ({})".format(session["title"],
                                   session["title_source"])),
        ("directory", short_path(session["cwd"])),
        ("source", source_label(session)),
        ("branch", session["branch"]),
        ("started", format_local(session["first"])),
        ("active", format_local(session["last"])),
        ("prompts", session["prompts"]),
        ("size", "{:.1f} MB".format(session["size"] / 1e6)),
        ("cost", session["cost"] and "${:.2f}".format(session["cost"])),
        ("status", live_status_detail(session)),
        ("continued", session["continued_in"]),
        ("file", short_path(session["path"])),
    ]
    rows.extend(("pr", url) for url in session["prs"])
    for label, value in rows:
        if value not in (None, "", []):
            print("{:<10} {}".format(paint(label, "2"), value))
    for label in ("first_prompt", "last_prompt"):
        print("\n" + paint(label.replace("_", " "), "2"))
        print(session[label] or "-")


def live_status_detail(session):
    """Return pid, kind and terminal of a running session."""
    live = session["live"]
    if not live:
        return None
    tty = process_tty(live["pid"])
    parts = ["pid {}".format(live["pid"]), live.get("kind") or "?",
             live.get("status") or "?"]
    if tty:
        parts.append("tty " + tty)
    return ", ".join(parts)


# Audit: patterns

# (kind, severity, regex). The first group, if any, is the secret part.
SECRET_PATTERNS = [
    ("private key", "high", re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
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

PLACEHOLDER_HINTS = ("xxx", "***", "...", "example", "changeme", "your",
                     "placeholder", "secret", "password", "redacted",
                     "dummy", "<", "$", "{", "%")
RE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
RE_SNAKE = re.compile(r"^[a-z_]+$|^[A-Z_]+$")

# (category, severity, regex) checked against Bash commands
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
AUDIT_CACHE = os.path.join(HOME, ".cache", "claude-sessions-audit.json")
AUDIT_VERSION = 5


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
        return value[:1] + "\u2026" + "({})".format(len(value))
    return "{}\u2026{} ({})".format(value[:4], value[-2:], len(value))


def fingerprint(value):
    """Return a short hash that identifies a secret without storing it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def secret_finding(kind, severity, match):
    """Return a finding for one regex match, or None for noise."""
    value = match.group(1) if match.groups() else match.group(0)
    checked = ("password assignment", "url credentials")
    if kind in checked and looks_like_placeholder(value):
        return None
    shown = value if kind == "private key" else mask(value)
    return {"group": "secret", "category": kind, "severity": severity,
            "detail": shown, "fp": fingerprint(value)}


def scan_secrets(line):
    """Yield secret findings in one raw transcript line."""
    for kind, severity, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(line):
            finding = secret_finding(kind, severity, match)
            if finding:
                yield finding


def record_origin(record):
    """Return where in the conversation a record sits."""
    if record.get("type") == "assistant":
        return "assistant"
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content):
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
    detail = " ".join(str(detail).split())[:160]
    return {"group": "action", "category": category, "severity": severity,
            "detail": detail, "fp": fingerprint(category + detail)}


def strip_heredocs(command):
    """Remove heredoc bodies: they are file content, not commands."""
    return RE_HEREDOC.sub("<<heredoc", command)


def excerpt(text, match):
    """Return the matched part of text with a little context."""
    start = max(match.start() - 30, 0)
    prefix = "\u2026" if start else ""
    return prefix + text[start: match.end() + 90]


def scan_command(command):
    """Yield findings for one Bash command."""
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
        yield from scan_command(str(tool_input.get("command", "")))
        if tool_input.get("dangerouslyDisableSandbox"):
            yield action_finding("sandbox disabled", "medium",
                                 tool_input.get("command", ""))
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


def audit_line(line, modes):
    """Return the findings of one raw transcript line."""
    if line.startswith('{"type":"permission-mode"'):
        record = parse_record(line)
        if record and record.get("permissionMode"):
            modes.add(record["permissionMode"])
        return []
    findings = list(scan_secrets(line))
    is_action = '"tool_use"' in line and '"type":"assistant"' in line
    if not findings and not is_action:
        return []
    record = parse_record(line) or {}
    if is_action:
        findings.extend(scan_actions(record))
    place = record_origin(record)
    stamp = record.get("timestamp")
    for finding in findings:
        finding.setdefault("where", place)
        finding["time"] = stamp
    return findings


def merge_finding(found, finding):
    """Count repeated findings instead of listing them twice."""
    key = (finding["group"], finding["category"], finding["fp"])
    if key in found:
        found[key]["count"] += 1
        return
    finding["count"] = 1
    found[key] = finding


def audit_transcript(path):
    """Return (findings, permission modes) of one transcript."""
    found = {}
    modes = set()
    with open(path, errors="replace") as handle:
        for line in handle:
            if not is_scannable(line):
                continue
            for finding in audit_line(line, modes):
                merge_finding(found, finding)
    if "bypassPermissions" in modes:
        merge_finding(found, action_finding(
            "permissions bypassed", "high", "bypassPermissions mode"))
    return list(found.values()), sorted(modes)


def load_audit_cache():
    """Return cached audit results keyed by transcript path."""
    try:
        with open(AUDIT_CACHE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if data.get("version") != AUDIT_VERSION:
        return {}
    return data.get("entries", {})


def save_audit_cache(entries):
    """Write the audit cache; it holds masked values only."""
    os.makedirs(os.path.dirname(AUDIT_CACHE), exist_ok=True)
    temp = AUDIT_CACHE + ".tmp"
    with open(temp, "w") as handle:
        json.dump({"version": AUDIT_VERSION, "entries": entries}, handle)
    os.chmod(temp, 0o600)
    os.replace(temp, AUDIT_CACHE)


def cached_audit(session, cache):
    """Return the audit result of a session, scanning if stale."""
    key = file_key(session["path"])
    entry = cache.get(session["path"])
    if entry and entry.get("key") == key:
        return entry["result"]
    findings, modes = audit_transcript(session["path"])
    result = {"findings": findings, "modes": modes}
    cache[session["path"]] = {"key": key, "result": result}
    return result


# Audit: rendering

def wanted_finding(finding, args):
    """Return True when a finding passes the audit filters."""
    if args.only and finding["group"] != args.only:
        return False
    rank = SEVERITY_ORDER[finding["severity"]]
    return rank <= SEVERITY_ORDER[args.min_severity]


def finding_sort_key(finding):
    """Order findings by severity, then group and category."""
    return (SEVERITY_ORDER[finding["severity"]], finding["group"],
            finding["category"])


def format_finding(finding):
    """Return one indented report line for a finding."""
    severity = finding["severity"]
    count = "x{}".format(finding["count"]) if finding["count"] > 1 else ""
    return "    {} {} {} {} {}".format(
        paint(severity.ljust(6), SEVERITY_COLOR[severity]),
        clip(finding["category"], 20),
        paint(clip(finding.get("where") or "", 11), "2"),
        count.rjust(4),
        finding["detail"])


def print_audit(session, result, findings):
    """Print the report block of one session."""
    print(format_row(session))
    modes = ", ".join(result["modes"])
    if modes:
        print("    " + paint("modes  " + modes, "2"))
    for finding in findings:
        print(format_finding(finding))
    print()


def audit_session(session, args, cache):
    """Audit one session and print it; return its shown findings."""
    result = cached_audit(session, cache)
    findings = sorted((f for f in result["findings"]
                       if wanted_finding(f, args)), key=finding_sort_key)
    if findings:
        print_audit(session, result, findings[: args.max])
    return findings


def print_audit_summary(all_findings, session_count):
    """Print totals per severity and distinct secrets."""
    totals = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in all_findings:
        totals[finding["severity"]] += 1
    secrets = {f["fp"] for f in all_findings if f["group"] == "secret"}
    print("{} sessions scanned: {} high, {} medium, {} low, "
          "{} distinct secrets".format(session_count, totals["high"],
                                       totals["medium"], totals["low"],
                                       len(secrets)))


# Commands

def cmd_audit(args, sessions):
    """Scan transcripts for secrets and risky actions."""
    args.all = True
    selected = select_sessions(args, sessions)
    cache = load_audit_cache()
    all_findings = []
    for session in selected:
        all_findings.extend(audit_session(session, args, cache))
    cache = {p: cache[p] for p in cache if os.path.exists(p)}
    save_audit_cache(cache)
    print_audit_summary(all_findings, len(selected))


def review_directory(sessions, ref):
    """Return the directory and label a review should run in."""
    if os.path.isdir(ref):
        path = os.path.abspath(ref)
        return path, os.path.basename(path)
    session = resolve(sessions, ref, newest=True)
    return session["cwd"], session["title"] or session["id"][:8]


def is_git_repo(path):
    """Return True when path lies inside a git work tree."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True)
    return result.returncode == 0


def cmd_review(args, sessions):
    """Start a new claude session running /security-review."""
    path, label = review_directory(sessions, args.ref)
    if not os.path.isdir(path):
        fail("directory is gone: " + path)
    if not is_git_repo(path):
        fail("not a git repository: " + short_path(path))
    os.chdir(path)
    print("cd " + short_path(path), file=sys.stderr)
    prompt = "/security-review"
    if args.focus:
        prompt += " " + " ".join(args.focus)
    argv = ["claude", "-n", "security-review " + clip(label, 40).strip(),
            prompt]
    os.execvp(argv[0], argv)


def cmd_list(args, sessions):
    """Print the session table."""
    if sys.stdout.isatty():
        print(header_row())
    for session in select_sessions(args, sessions):
        print(format_row(session))


def cmd_show(args, sessions):
    """Print details of one session."""
    show_session(resolve(sessions, args.ref, newest=True))


def resume_argv(session, fork):
    """Return the claude command line that reopens a session."""
    live = session["live"]
    if live and live.get("kind") == "bg":
        return ["claude", "attach", session["id"]]
    argv = ["claude", "--resume", session["id"]]
    if fork:
        argv.append("--fork-session")
    return argv


def cmd_resume(args, sessions):
    """Change into the session directory and exec claude."""
    session = resolve(sessions, args.ref, newest=True)
    live = session["live"]
    if live and live.get("kind") != "bg" and not args.fork:
        fail("session is open in another terminal ({}).\n"
             "Use --fork to branch off a copy."
             .format(live_status_detail(session)))
    if not os.path.isdir(session["cwd"]):
        fail("directory is gone: " + session["cwd"])
    os.chdir(session["cwd"])
    print("cd " + short_path(session["cwd"]), file=sys.stderr)
    argv = resume_argv(session, args.fork)
    os.execvp(argv[0], argv)


def ensure_idle(session):
    """Exit when a session is running and must not be touched."""
    if session["live"]:
        fail("session {} is running ({}), close it first"
             .format(session["id"][:8], live_status_detail(session)))


def cmd_rename(args, sessions):
    """Append a custom-title record to a transcript."""
    session = resolve(sessions, args.ref)
    ensure_idle(session)
    record = {"type": "custom-title", "customTitle": args.name,
              "sessionId": session["id"]}
    with open(session["path"], "a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    print("renamed {} to {!r}".format(session["id"][:8], args.name))


def confirm(question):
    """Ask a yes/no question on the terminal."""
    try:
        answer = input(question + " [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def trash_session(session):
    """Move a transcript and its sidecar directory to the Trash."""
    target = os.path.join(TRASH, os.path.basename(
        os.path.dirname(session["path"])))
    os.makedirs(target, exist_ok=True)
    sidecar = session["path"][: -len(".jsonl")]
    for path in (session["path"], sidecar):
        if os.path.exists(path):
            shutil.move(path, os.path.join(target, os.path.basename(path)))


def cmd_rm(args, sessions):
    """Move sessions to the Trash after confirmation."""
    selected = [resolve(sessions, ref) for ref in args.refs]
    for session in selected:
        ensure_idle(session)
        print(format_row(session))
    if not args.yes and not confirm("Move to Trash?"):
        return
    for session in selected:
        trash_session(session)
    print("moved {} session(s) to {}".format(
        len(selected), short_path(TRASH)))


def line_snippet(line, needle):
    """Return text around needle from a matching message line."""
    record = parse_record(line)
    if not record or record.get("type") not in ("user", "assistant"):
        return None
    text = message_text((record.get("message") or {}).get("content"))
    index = text.lower().find(needle)
    if index < 0:
        return None
    start = max(index - 50, 0)
    return " ".join(text[start: index + len(needle) + 70].split())


def grep_session(session, needle):
    """Return up to three snippets of needle in one transcript."""
    hits = []
    with open(session["path"], errors="replace") as handle:
        for line in handle:
            if needle not in line.lower():
                continue
            snippet = line_snippet(line, needle)
            if snippet:
                hits.append(snippet)
            if len(hits) >= 3:
                break
    return hits


def cmd_grep(args, sessions):
    """Search the message text of all transcripts."""
    needle = " ".join(args.text).lower()
    for session in sessions:
        hits = grep_session(session, needle)
        if not hits:
            continue
        print(format_row(session))
        for hit in hits:
            print("    " + paint("…" + hit + "…", "2"))


def fzf_line(session):
    """Return one picker line: id, tab, plain row text."""
    return session["id"] + "\t" + format_row(session)


def cmd_pick(args, sessions):
    """Choose a session with fzf and resume it."""
    if not shutil.which("fzf"):
        fail("fzf is not installed")
    selected = select_sessions(args, sessions)
    os.environ["NO_COLOR"] = "1"
    lines = "\n".join(fzf_line(s) for s in selected)
    me = os.path.abspath(sys.argv[0])
    result = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2",
         "--no-sort", "--prompt", "session> ",
         "--preview", "NO_COLOR=1 {} show {{1}}".format(me),
         "--preview-window", "down,45%,wrap",
         "--header", "Enter resume, ctrl-y copy id"],
        input=lines, capture_output=False, stdout=subprocess.PIPE,
        text=True)
    choice = result.stdout.strip()
    if not choice:
        return
    os.environ.pop("NO_COLOR", None)
    args.ref = choice.split("\t", 1)[0]
    args.fork = False
    cmd_resume(args, sessions)


# Argument parsing

def filter_parser():
    """Return a parent parser with the shared list filters."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("query", nargs="*")
    parent.add_argument("-a", "--all", action="store_true",
                        help="include empty and continued sessions")
    parent.add_argument("-n", "--limit", type=int, default=0)
    parent.add_argument("--here", action="store_true",
                        help="only sessions in or below this directory")
    named = parent.add_mutually_exclusive_group()
    named.add_argument("--named", dest="named", action="store_true",
                       default=None, help="only sessions you named")
    named.add_argument("--unnamed", dest="named", action="store_false",
                       help="only sessions without a name")
    source = parent.add_mutually_exclusive_group()
    source.add_argument("--app", dest="origin", action="store_const",
                        const="app", help="started in the Claude app")
    source.add_argument("--term", dest="origin", action="store_const",
                        const="term", help="started in a terminal")
    parent.add_argument("-s", "--sort", choices=sorted(SORT_KEYS),
                        default="active")
    parent.add_argument("-r", "--reverse", action="store_true")
    return parent


def build_parser():
    """Return the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="claudes", description="Manage Claude Code sessions.")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command")
    filters = filter_parser()

    listing = sub.add_parser("list", aliases=["ls"], parents=[filters],
                             help="list sessions")
    listing.set_defaults(func=cmd_list)

    pick = sub.add_parser("pick", aliases=["p"], parents=[filters],
                          help="fzf picker")
    pick.set_defaults(func=cmd_pick)

    show = sub.add_parser("show", help="session details")
    show.add_argument("ref")
    show.set_defaults(func=cmd_show)

    resume = sub.add_parser("resume", aliases=["r"], help="resume")
    resume.add_argument("ref")
    resume.add_argument("--fork", action="store_true",
                        help="branch off into a new session id")
    resume.set_defaults(func=cmd_resume)

    rename = sub.add_parser("rename", help="set the session title")
    rename.add_argument("ref")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_rename)

    remove = sub.add_parser("rm", help="move sessions to Trash")
    remove.add_argument("refs", nargs="+")
    remove.add_argument("-y", "--yes", action="store_true")
    remove.set_defaults(func=cmd_rm)

    grep = sub.add_parser("grep", help="full text search")
    grep.add_argument("text", nargs="+")
    grep.set_defaults(func=cmd_grep)

    audit = sub.add_parser("audit", parents=[filters],
                           help="secrets and risky actions")
    audit.add_argument("--only", choices=["secret", "action"])
    audit.add_argument("--min-severity", choices=sorted(SEVERITY_ORDER),
                       default="medium")
    audit.add_argument("--max", type=int, default=15,
                       help="findings shown per session")
    audit.set_defaults(func=cmd_audit)

    review = sub.add_parser("review", help="run /security-review")
    review.add_argument("ref", help="session id, title or directory")
    review.add_argument("focus", nargs="*",
                        help="extra instructions for the review")
    review.set_defaults(func=cmd_review)
    return parser


COMMANDS = {"list", "ls", "pick", "p", "show", "resume", "r", "rename",
            "rm", "grep", "audit", "review", "-h", "--help",
            "--version"}


def main():
    """Entry point: bare queries default to the list command."""
    argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        argv = ["list"] + argv
    args = build_parser().parse_args(argv)
    args.func(args, load_sessions())


def run():
    """Console entry point: exit quietly on a closed pipe or ctrl-c."""
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(130)


if __name__ == "__main__":
    run()
