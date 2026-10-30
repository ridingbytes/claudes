"""Selecting many sessions: age and directory filters, pipes, bulk rm."""

import argparse
import datetime
import io
import json
import os

import pytest

import claudes
from conftest import make_id, user_line


def iso_days_ago(days):
    """Return an ISO timestamp the given number of days back."""
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now - datetime.timedelta(days=days)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def piped(monkeypatch):
    """Return a function that feeds text to stdin as a pipe would."""
    def feed(text):
        monkeypatch.setattr(claudes.sys, "stdin", io.StringIO(text))
    return feed


@pytest.fixture
def three_sessions(make_session):
    """An old unnamed session in home, a fresh named one, a project."""
    make_session(make_id(1), [
        user_line("old home chat", cwd="/home/me",
                  stamp=iso_days_ago(45))])
    make_session(make_id(2), [
        user_line("fresh", cwd="/home/me", stamp=iso_days_ago(1)),
        {"type": "custom-title", "customTitle": "keeper"}])
    make_session(make_id(3), [
        user_line("project work", cwd="/home/me/develop/api",
                  stamp=iso_days_ago(40))])
    return claudes.load_sessions()


def listed_ids(capsys, argv):
    """Run claudes with argv and return the printed ids."""
    claudes.main(argv)
    return capsys.readouterr().out.split()


def test_parse_duration():
    assert claudes.parse_duration("30d") == 30 * 86400
    assert claudes.parse_duration("2w") == 14 * 86400
    with pytest.raises(argparse.ArgumentTypeError):
        claudes.parse_duration("30 days")


def test_older_and_newer(three_sessions, capsys):
    older = listed_ids(capsys, ["--older", "30d", "-q"])
    assert older == [make_id(3), make_id(1)]
    assert listed_ids(capsys, ["--newer", "7d", "-q"]) == [make_id(2)]


def test_dir_is_exact_and_under_includes_subdirectories(
        three_sessions, capsys):
    exact = listed_ids(capsys, ["--dir", "/home/me", "-q"])
    assert sorted(exact) == [make_id(1), make_id(2)]
    under = listed_ids(capsys, ["--under", "/home/me", "-q"])
    assert len(under) == 3


def test_format_template(three_sessions, capsys):
    claudes.main(["--named", "--format", "{short_id} {title}"])
    assert capsys.readouterr().out == "00000002 keeper\n"


def test_bad_format_names_the_fields(three_sessions, capsys):
    with pytest.raises(SystemExit):
        claudes.main(["--format", "{nope}"])
    assert "short_id" in capsys.readouterr().err


def test_ids_from_accepts_ids_and_table_rows(three_sessions, piped,
                                             capsys):
    piped(make_id(1) + "\n00000003  40d   term  project work  ~  1\n")
    ids = listed_ids(capsys, ["--from", "-", "-q"])
    assert sorted(ids) == [make_id(1), make_id(3)]


def test_rm_with_filters(three_sessions, capsys):
    claudes.main(["rm", "-y", "--unnamed", "--dir", "/home/me",
                  "--older", "30d"])
    remaining = [s.id for s in claudes.load_sessions()]
    assert remaining == [make_id(2), make_id(3)]


def test_rm_from_pipe(three_sessions, piped):
    piped(make_id(3) + "\n")
    claudes.main(["rm", "-y", "-"])
    assert make_id(3) not in [s.id for s in claudes.load_sessions()]


def test_list_to_rm_round_trip(three_sessions, piped, capsys):
    claudes.main(["--older", "30d", "-q"])
    piped(capsys.readouterr().out)
    claudes.main(["rm", "-y", "-"])
    assert [s.id for s in claudes.load_sessions()] == [make_id(2)]


def test_piped_id_never_guesses(three_sessions, piped, capsys):
    piped("0000\n")
    with pytest.raises(SystemExit):
        claudes.main(["rm", "-y", "-"])
    assert "matches 3 sessions" in capsys.readouterr().err


def test_rm_needs_a_selection(three_sessions, capsys):
    with pytest.raises(SystemExit):
        claudes.main(["rm", "-y"])
    assert "which sessions" in capsys.readouterr().err


def test_rm_filters_skip_running_sessions(three_sessions, claude_home):
    entry = {"pid": os.getpid(), "sessionId": make_id(1),
             "kind": "interactive", "status": "idle"}
    (claude_home / "sessions" / "1.json").write_text(json.dumps(entry))
    claudes.main(["rm", "-y", "--older", "30d"])
    remaining = [s.id for s in claudes.load_sessions()]
    assert make_id(1) in remaining
    assert make_id(3) not in remaining


def test_rm_without_terminal_requires_yes(three_sessions, monkeypatch,
                                          capsys):
    def no_terminal(question):
        raise OSError("no tty")
    monkeypatch.setattr(claudes, "ask", no_terminal)
    with pytest.raises(SystemExit):
        claudes.main(["rm", "--older", "30d"])
    assert "add -y" in capsys.readouterr().err
    assert len(claudes.load_sessions()) == 3


def test_show_many_from_pipe(three_sessions, piped, capsys):
    piped(make_id(1) + "\n" + make_id(2) + "\n")
    claudes.main(["show", "-"])
    out = capsys.readouterr().out
    assert "old home chat" in out and "keeper" in out
