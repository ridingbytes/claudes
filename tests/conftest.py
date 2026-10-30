"""Shared fixtures: a fake ~/.claude tree with handwritten transcripts."""

import json
import os

import pytest

import claudes


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Point claudes at an empty fake home and return its root."""
    projects = tmp_path / "projects"
    registry = tmp_path / "sessions"
    projects.mkdir()
    registry.mkdir()
    monkeypatch.setattr(claudes, "PROJECTS", str(projects))
    monkeypatch.setattr(claudes, "REGISTRY", str(registry))
    monkeypatch.setattr(claudes, "CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(claudes, "AUDIT_CACHE",
                        str(tmp_path / "audit.json"))
    monkeypatch.setattr(claudes, "TRASH", str(tmp_path / "trash"))
    return tmp_path


def user_line(text, cwd="/work/demo", stamp="2026-01-01T10:00:00Z",
              entrypoint="cli", **extra):
    """Return a user record with a human prompt."""
    record = {
        "parentUuid": None, "type": "user",
        "message": {"role": "user", "content": text},
        "cwd": cwd, "timestamp": stamp, "entrypoint": entrypoint,
        "gitBranch": "main",
    }
    record.update(extra)
    return record


def bash_line(command, stamp="2026-01-01T10:01:00Z", **tool_input):
    """Return an assistant record that runs one Bash command."""
    tool_input["command"] = command
    return {
        "parentUuid": None, "type": "assistant", "timestamp": stamp,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": tool_input}]},
    }


def tool_line(name, tool_input, stamp="2026-01-01T10:01:00Z"):
    """Return an assistant record that calls any tool."""
    return {
        "parentUuid": None, "type": "assistant", "timestamp": stamp,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": name, "input": tool_input}]},
    }


def tool_output_line(text, stamp="2026-01-01T10:02:00Z"):
    """Return a user record carrying a tool result."""
    return {
        "parentUuid": None, "type": "user", "timestamp": stamp,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": text}]},
    }


def write_session(root, session_id, records, slug="-work-demo"):
    """Write records as a transcript and return its path."""
    project = os.path.join(root, "projects", slug)
    os.makedirs(project, exist_ok=True)
    path = os.path.join(project, session_id + ".jsonl")
    with open(path, "w") as handle:
        for record in records:
            # Claude Code writes compact JSON; the scanner relies on it
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


@pytest.fixture
def make_session(claude_home):
    """Return a factory that writes a transcript into the fake home."""
    def factory(session_id, records, slug="-work-demo"):
        return write_session(str(claude_home), session_id, records, slug)
    return factory


def make_id(number):
    """Return a valid session uuid ending in number."""
    return "00000000-0000-0000-0000-{:012d}".format(number)
