"""Transcript parsing, listing, filters, sorting, lookup and commands."""

import json
import os

import pytest

import claudes
from conftest import make_id, user_line
from claudes import Filters


def ids(sessions, **filters):
    """Return the ids select_sessions keeps for the given filters."""
    selected = claudes.select_sessions(sessions, Filters(**filters))
    return [session.id for session in selected]


def append_record(path, record):
    """Append one compact JSON record, as Claude Code writes them."""
    with open(path, "a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


@pytest.fixture
def no_exec(monkeypatch):
    """Record os.execvp calls instead of replacing the process."""
    calls = []
    monkeypatch.setattr(claudes.os, "execvp",
                        lambda name, argv: calls.append(argv))
    monkeypatch.setattr(claudes.os, "chdir", lambda path: None)
    return calls


@pytest.fixture
def picker(monkeypatch):
    """Pretend fzf is usable and pick a scripted choice."""
    offered = []

    def fake_select(sessions, query="", header=""):
        offered.append([s.id for s in sessions])
        return sessions[-1]

    monkeypatch.setattr(claudes, "can_pick", lambda: True)
    monkeypatch.setattr(claudes, "fzf_select", fake_select)
    return offered


# Parsing

def test_prompt_text_skips_meta_and_tags():
    plain = user_line("fix the parser")
    meta = user_line("caveat", isMeta=True)
    tagged = user_line("<local-command-stdout>ok</local-command-stdout>")
    command = user_line("<command-name>/review</command-name>")
    assert claudes.prompt_text(plain) == "fix the parser"
    assert claudes.prompt_text(meta) is None
    assert claudes.prompt_text(tagged) is None
    assert claudes.prompt_text(command) == "/review"


def test_read_meta_collects_titles_and_context(make_session):
    path = make_session(make_id(1), [
        user_line("first question", stamp="2026-01-01T10:00:00Z"),
        {"type": "ai-title", "aiTitle": "Parser fix"},
        user_line("second question", stamp="2026-01-02T10:00:00Z",
                  entrypoint="claude-desktop"),
        {"type": "custom-title", "customTitle": "parser"},
        {"type": "cost-state", "totalCostUSD": 1.5},
    ])
    meta = claudes.read_meta(path)
    assert meta.prompts == 2
    assert meta.first_prompt == "first question"
    assert meta.last_prompt == "second question"
    assert meta.cwd == "/work/demo"
    assert meta.first == "2026-01-01T10:00:00Z"
    assert meta.last == "2026-01-02T10:00:00Z"
    assert meta.entrypoints == ["cli", "claude-desktop"]
    assert meta.titles == {"ai": "Parser fix", "user": "parser"}
    assert meta.cost == 1.5


def test_title_priority():
    meta = claudes.Meta(first_prompt="hello")
    assert claudes.pick_title(meta) == ("hello", "prompt")
    meta.titles["ai"] = "AI"
    assert claudes.pick_title(meta) == ("AI", "ai")
    meta.titles["user"] = "Mine"
    assert claudes.pick_title(meta) == ("Mine", "user")


def test_source_shows_both_environments(make_session):
    make_session(make_id(1), [
        user_line("a"), user_line("b", entrypoint="claude-desktop")])
    session = claudes.load_sessions()[0]
    assert session.origin == "term"
    assert session.source == "term>app"


# Selecting

def test_continued_and_empty_sessions_are_hidden(make_session):
    make_session(make_id(1), [
        user_line("old"),
        {"type": "continued-in", "continuedInSessionId": make_id(2)},
    ])
    make_session(make_id(2), [user_line("new")])
    make_session(make_id(3), [{"type": "mode", "mode": "normal"}])
    sessions = claudes.load_sessions()
    assert ids(sessions) == [make_id(2)]
    assert len(ids(sessions, include_hidden=True)) == 3


def test_filters_named_origin_and_query(make_session):
    make_session(make_id(1), [
        user_line("app work", entrypoint="claude-desktop"),
        {"type": "custom-title", "customTitle": "mail"},
    ])
    make_session(make_id(2), [user_line("terminal work")])
    sessions = claudes.load_sessions()
    assert ids(sessions, named=True) == [make_id(1)]
    assert ids(sessions, named=False) == [make_id(2)]
    assert ids(sessions, origin="app") == [make_id(1)]
    assert ids(sessions, origin="term") == [make_id(2)]
    assert ids(sessions, query=["terminal"]) == [make_id(2)]


def test_here_filter(make_session):
    make_session(make_id(1), [user_line("a", cwd="/work/api")])
    make_session(make_id(2), [user_line("b", cwd="/work/apiary")])
    sessions = claudes.load_sessions()
    assert ids(sessions, directory="/work/api") == [make_id(1)]


def test_sorting(make_session):
    make_session(make_id(1), [
        user_line("b", stamp="2026-01-01T10:00:00Z"),
        user_line("b again", stamp="2026-01-01T11:00:00Z"),
        {"type": "custom-title", "customTitle": "zeta"},
    ])
    make_session(make_id(2), [
        user_line("a", stamp="2026-02-01T10:00:00Z"),
        {"type": "custom-title", "customTitle": "alpha"},
    ])
    sessions = claudes.load_sessions()
    assert ids(sessions) == [make_id(2), make_id(1)]
    assert ids(sessions, sort="name") == [make_id(2), make_id(1)]
    assert ids(sessions, sort="name", reverse=True) == [
        make_id(1), make_id(2)]
    assert ids(sessions, sort="msgs") == [make_id(1), make_id(2)]


# Finding one session

def two_sessions_named(make_session, title):
    """Write two sessions sharing a title; the second is newer."""
    for number, stamp in ((1, "2026-01-01T10:00:00Z"),
                          (2, "2026-03-01T10:00:00Z")):
        make_session(make_id(number), [
            user_line("x", stamp=stamp),
            {"type": "custom-title", "customTitle": title},
        ])
    return claudes.load_sessions()


def test_resolve_by_prefix_and_newest_title(make_session):
    sessions = two_sessions_named(make_session, "same")
    assert claudes.resolve(sessions, make_id(1)).id == make_id(1)
    newest = claudes.resolve(sessions, "same", newest=True)
    assert newest.id == make_id(2)


def test_ambiguous_ref_without_terminal_lists_candidates(
        make_session, monkeypatch, capsys):
    sessions = two_sessions_named(make_session, "same")
    monkeypatch.setattr(claudes, "can_pick", lambda: False)
    with pytest.raises(SystemExit):
        claudes.resolve(sessions, "same")
    assert "matches 2 sessions" in capsys.readouterr().err


def test_ambiguous_ref_opens_picker(make_session, picker):
    sessions = two_sessions_named(make_session, "same")
    chosen = claudes.resolve(sessions, "sam")
    assert picker == [[make_id(2), make_id(1)]]
    assert chosen.id == make_id(1)


# Running sessions

def register_process(claude_home, session_id, kind="interactive"):
    """Write a registry entry for this test process."""
    entry = {"pid": os.getpid(), "sessionId": session_id, "kind": kind,
             "status": "idle", "entrypoint": "cli"}
    path = claude_home / "sessions" / "1.json"
    path.write_text(json.dumps(entry))


def test_registry_marks_running_sessions(make_session, claude_home):
    make_session(make_id(1), [user_line("x")])
    register_process(claude_home, make_id(1))
    session = claudes.load_sessions()[0]
    assert session.process.label == "run:idle"


# Commands

def test_dash_r_resumes_like_claude(make_session, no_exec):
    make_session(make_id(1), [user_line("x", cwd="/")])
    claudes.main(["-r", make_id(1)[:8]])
    assert no_exec == [["claude", "--resume", make_id(1)]]


def test_dash_r_without_id_opens_picker(make_session, no_exec, picker):
    make_session(make_id(1), [user_line("x", cwd="/")])
    claudes.main(["-r"])
    assert picker == [[make_id(1)]]
    assert no_exec == [["claude", "--resume", make_id(1)]]


def test_resume_refuses_an_open_session(make_session, claude_home,
                                        no_exec, capsys):
    make_session(make_id(1), [user_line("x", cwd="/")])
    register_process(claude_home, make_id(1))
    with pytest.raises(SystemExit):
        claudes.main(["-r", make_id(1)[:8]])
    assert "open in another terminal" in capsys.readouterr().err
    claudes.main(["-r", make_id(1)[:8], "--fork"])
    assert no_exec == [["claude", "--resume", make_id(1),
                        "--fork-session"]]


def test_background_session_is_attached(make_session, claude_home,
                                        no_exec):
    make_session(make_id(1), [user_line("x", cwd="/")])
    register_process(claude_home, make_id(1), kind="bg")
    claudes.main(["-r", make_id(1)[:8]])
    assert no_exec == [["claude", "attach", make_id(1)]]


def test_bare_words_list_and_json(make_session, capsys):
    make_session(make_id(1), [user_line("upload bug")])
    make_session(make_id(2), [user_line("other")])
    claudes.main(["upload", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in listed] == [make_id(1)]
    assert listed[0]["title"] == "upload bug"


def test_help_is_global_not_list(capsys):
    with pytest.raises(SystemExit):
        claudes.main(["-h"])
    assert "COMMAND" in capsys.readouterr().out


def test_rename_appends_custom_title(make_session):
    path = make_session(make_id(1), [user_line("x")])
    claudes.main(["rename", make_id(1)[:8], "renamed"])
    assert claudes.read_meta(path).titles["user"] == "renamed"


def test_rm_moves_transcript_and_sidecar(make_session, claude_home):
    path = make_session(make_id(1), [user_line("x")])
    os.makedirs(path[:-6])
    claudes.main(["rm", "-y", make_id(1)])
    assert not os.path.exists(path)
    trashed = claude_home / "trash" / "-work-demo"
    assert (trashed / (make_id(1) + ".jsonl")).exists()
    assert (trashed / make_id(1)).is_dir()


def test_rm_empty_takes_only_sessions_without_prompts(make_session):
    kept = make_session(make_id(1), [user_line("x")])
    empty = make_session(make_id(2), [{"type": "mode", "mode": "normal"}])
    claudes.main(["rm", "-y", "--empty"])
    assert os.path.exists(kept)
    assert not os.path.exists(empty)


def test_grep_respects_filters(make_session, capsys):
    make_session(make_id(1), [user_line("the connection pool leaks")])
    make_session(make_id(2), [
        user_line("connection pool in the app",
                  entrypoint="claude-desktop")])
    claudes.main(["grep", "connection", "pool", "--term"])
    out = capsys.readouterr().out
    assert make_id(1)[:8] in out
    assert make_id(2)[:8] not in out


# Cache

def test_cache_is_reused_until_the_file_changes(make_session):
    path = make_session(make_id(1), [user_line("x")])
    assert claudes.load_sessions()[0].prompts == 1
    append_record(path, user_line("y"))
    assert claudes.load_sessions()[0].prompts == 2


def test_caches_are_owner_only(make_session, claude_home):
    make_session(make_id(1), [user_line("private prompt")])
    claudes.load_sessions()
    mode = os.stat(claude_home / "cache.json").st_mode & 0o777
    assert mode == 0o600
