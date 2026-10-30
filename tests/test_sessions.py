"""Transcript parsing, listing, filters, sorting and lookup."""

import argparse
import json
import os

import pytest

import claudes
from conftest import make_id, user_line


def list_args(**overrides):
    """Return list arguments with defaults, as the parser builds them."""
    values = {"query": [], "all": False, "limit": 0, "here": False,
              "named": None, "origin": None, "sort": "active",
              "reverse": False}
    values.update(overrides)
    return argparse.Namespace(**values)


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
    assert meta["prompts"] == 2
    assert meta["first_prompt"] == "first question"
    assert meta["last_prompt"] == "second question"
    assert meta["cwd"] == "/work/demo"
    assert meta["first"] == "2026-01-01T10:00:00Z"
    assert meta["last"] == "2026-01-02T10:00:00Z"
    assert meta["entrypoints"] == ["cli", "claude-desktop"]
    assert meta["titles"] == {"ai": "Parser fix", "user": "parser"}
    assert meta["cost"] == 1.5


def test_title_priority():
    meta = claudes.empty_meta()
    meta["first_prompt"] = "hello"
    assert claudes.pick_title(meta) == ("hello", "prompt")
    meta["titles"]["ai"] = "AI"
    assert claudes.pick_title(meta) == ("AI", "ai")
    meta["titles"]["user"] = "Mine"
    assert claudes.pick_title(meta) == ("Mine", "user")


def test_continued_and_empty_sessions_are_hidden(make_session):
    make_session(make_id(1), [
        user_line("old"),
        {"type": "continued-in", "continuedInSessionId": make_id(2)},
    ])
    make_session(make_id(2), [user_line("new")])
    make_session(make_id(3), [{"type": "mode", "mode": "normal"}])
    sessions = claudes.load_sessions()
    shown = claudes.select_sessions(list_args(), sessions)
    assert [s["id"] for s in shown] == [make_id(2)]
    everything = claudes.select_sessions(list_args(all=True), sessions)
    assert len(everything) == 3


def test_filters_named_origin_and_query(make_session):
    make_session(make_id(1), [
        user_line("app work", entrypoint="claude-desktop"),
        {"type": "custom-title", "customTitle": "mail"},
    ])
    make_session(make_id(2), [user_line("terminal work")])
    sessions = claudes.load_sessions()

    def ids(**overrides):
        selected = claudes.select_sessions(list_args(**overrides), sessions)
        return [s["id"] for s in selected]

    assert ids(named=True) == [make_id(1)]
    assert ids(named=False) == [make_id(2)]
    assert ids(origin="app") == [make_id(1)]
    assert ids(origin="term") == [make_id(2)]
    assert ids(query=["terminal"]) == [make_id(2)]


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

    def ids(**overrides):
        selected = claudes.select_sessions(list_args(**overrides), sessions)
        return [s["id"] for s in selected]

    assert ids() == [make_id(2), make_id(1)]
    assert ids(sort="name") == [make_id(2), make_id(1)]
    assert ids(sort="name", reverse=True) == [make_id(1), make_id(2)]
    assert ids(sort="msgs") == [make_id(1), make_id(2)]


def test_resolve_by_prefix_and_title(make_session, capsys):
    make_session(make_id(1), [
        user_line("x", stamp="2026-01-01T10:00:00Z"),
        {"type": "custom-title", "customTitle": "same"},
    ])
    make_session(make_id(2), [
        user_line("y", stamp="2026-03-01T10:00:00Z"),
        {"type": "custom-title", "customTitle": "same"},
    ])
    sessions = claudes.load_sessions()
    assert claudes.resolve(sessions, make_id(1))["id"] == make_id(1)
    newest = claudes.resolve(sessions, "same", newest=True)
    assert newest["id"] == make_id(2)
    with pytest.raises(SystemExit):
        claudes.resolve(sessions, "same")
    assert "ambiguous" in capsys.readouterr().err


def test_live_registry_marks_running_sessions(make_session, claude_home):
    make_session(make_id(1), [user_line("x")])
    entry = {"pid": os.getpid(), "sessionId": make_id(1),
             "kind": "interactive", "status": "idle", "entrypoint": "cli"}
    registry = claude_home / "sessions" / "1.json"
    registry.write_text(json.dumps(entry))
    session = claudes.load_sessions()[0]
    assert claudes.live_label(session) == "run:idle"


def test_rename_appends_custom_title(make_session):
    path = make_session(make_id(1), [user_line("x")])
    sessions = claudes.load_sessions()
    args = argparse.Namespace(ref=make_id(1)[:8], name="renamed")
    claudes.cmd_rename(args, sessions)
    assert claudes.read_meta(path)["titles"]["user"] == "renamed"


def test_rm_moves_transcript_and_sidecar(make_session, claude_home):
    path = make_session(make_id(1), [user_line("x")])
    os.makedirs(path[:-6])
    sessions = claudes.load_sessions()
    args = argparse.Namespace(refs=[make_id(1)], yes=True)
    claudes.cmd_rm(args, sessions)
    assert not os.path.exists(path)
    trashed = claude_home / "trash" / "-work-demo"
    assert (trashed / (make_id(1) + ".jsonl")).exists()
    assert (trashed / make_id(1)).is_dir()


def test_cache_is_reused_until_the_file_changes(make_session):
    path = make_session(make_id(1), [user_line("x")])
    assert claudes.load_sessions()[0]["prompts"] == 1
    with open(path, "a") as handle:
        handle.write(json.dumps(user_line("y"), separators=(",", ":"))
                     + "\n")
    assert claudes.load_sessions()[0]["prompts"] == 2


def test_caches_are_owner_only(make_session, claude_home):
    make_session(make_id(1), [user_line("private prompt")])
    claudes.load_sessions()
    mode = os.stat(claude_home / "cache.json").st_mode & 0o777
    assert mode == 0o600
