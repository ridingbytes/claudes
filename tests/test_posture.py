"""Posture: risky allow rules, missing protections and --fix."""

import json
import os

import pytest

import claudes
from conftest import make_id, user_line


@pytest.fixture
def settings_home(claude_home, monkeypatch):
    """Point the settings paths at the fake home and return it."""
    user = claude_home / "settings.json"
    monkeypatch.setattr(claudes, "HOME", str(claude_home))
    monkeypatch.setattr(claudes, "USER_SETTINGS", str(user))
    monkeypatch.setattr(claudes, "CLAUDE_JSON",
                        str(claude_home / "claude.json"))
    monkeypatch.setattr(claudes, "MANAGED_SETTINGS",
                        (str(claude_home / "managed.json"),))
    return claude_home


def write_settings(path, data):
    """Write a settings file, creating its directory."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle)
    return str(path)


def allow(*rules, **extra):
    """Return settings with the given allow rules."""
    permissions = {"allow": list(rules)}
    permissions.update(extra)
    return {"permissions": permissions}


@pytest.mark.parametrize("rule, category", [
    ("Bash", "runs anything"),
    ("Bash(*)", "runs anything"),
    ("Bash(python3:*)", "runs anything"),
    ("Bash(python3 *)", "runs anything"),
    ("Bash(python -c *)", "runs anything"),
    ("Bash(/bin/zsh:*)", "runs anything"),
    ("Bash(env -i HOME=/home/me PATH=/usr/bin zsh:*)", "runs anything"),
    ("Bash(node *)", "runs anything"),
    ("Bash(ssh host *)", "remote shell"),
    ("Bash(ssh:*)", "remote shell"),
    ("Bash(docker exec *)", "remote shell"),
    ("Bash(find:*)", "runs anything (indirect)"),
    ("Bash(git push *)", "pushes code"),
    ("Bash(brew install *)", "installs software"),
    ("Bash(cat:*)", "reads any file"),
    ("Bash(curl *)", "network tool"),
    ("WebFetch", "fetches any url"),
    ("mcp__mail", "every mcp tool"),
])
def test_classify_allow_flags_risky_rules(rule, category):
    assert claudes.classify_allow(rule)[0] == category


@pytest.mark.parametrize("rule", [
    "Bash(python -m pytest:*)",
    "Bash(xargs du:*)",
    "Bash(ssh host uptime)",
    "Bash(git status)",
    "Bash(pyenv which:*)",
    "WebFetch(domain:github.com)",
    "mcp__mail__search",
    "Read(~/notes/**)",
])
def test_classify_allow_passes_narrow_rules(rule):
    assert claudes.classify_allow(rule) is None


def categories(findings):
    """Return the categories of findings, in order."""
    return [f.category for f in findings]


def test_posture_reports_missing_deny_and_sandbox(settings_home):
    write_settings(settings_home / "settings.json", allow("Bash(ls)"))
    findings, _ = claudes.posture_findings([])
    assert "no deny rules" in categories(findings)
    assert "sandbox off" in categories(findings)


def test_posture_accepts_deny_rules_and_sandbox(settings_home):
    settings = {"permissions": {"deny": ["Read(~/.ssh/**)"]},
                "sandbox": {"enabled": True, "filesystem": {
                    "denyRead": ["~/.ssh"]}}}
    write_settings(settings_home / "settings.json", settings)
    findings, _ = claudes.posture_findings([])
    assert categories(findings) == []


def test_project_deny_rules_do_not_count_everywhere(settings_home,
                                                    make_session):
    project = settings_home / "work"
    write_settings(project / ".claude" / "settings.json",
                   {"permissions": {"deny": ["Read(~/.ssh/**)"]}})
    make_session(make_id(1), [user_line("hi", cwd=str(project))])
    findings, _ = claudes.posture_findings(claudes.load_sessions())
    assert "no deny rules" in categories(findings)


def test_posture_reads_project_local_settings(settings_home, make_session):
    project = settings_home / "work"
    path = write_settings(project / ".claude" / "settings.local.json",
                          allow("Bash(*)"))
    make_session(make_id(1), [user_line("hi", cwd=str(project))])
    findings, paths = claudes.posture_findings(claudes.load_sessions())
    assert path in paths
    hit = next(f for f in findings if f.category == "runs anything")
    assert hit.severity == "high"
    assert hit.detail == "Bash(*)"


def test_bypass_mode_is_high_only_in_user_settings(settings_home,
                                                   make_session):
    write_settings(settings_home / "settings.json",
                   allow(defaultMode="bypassPermissions"))
    project = settings_home / "work"
    write_settings(project / ".claude" / "settings.local.json",
                   allow(defaultMode="bypassPermissions"))
    make_session(make_id(1), [user_line("hi", cwd=str(project))])
    findings, _ = claudes.posture_findings(claudes.load_sessions())
    modes = sorted(f.severity for f in findings
                   if f.category == "bypass mode")
    assert modes == ["high", "medium"]


def test_posture_lists_mcp_servers(settings_home):
    write_settings(settings_home / "claude.json", {
        "mcpServers": {"mail": {"command": "npx", "args": ["mail-mcp"]}},
        "projects": {"/work": {"mcpServers": {
            "db": {"url": "http://localhost:9/mcp"}}}},
    })
    findings, _ = claudes.posture_findings([])
    details = [f.detail for f in findings if f.category == "mcp server"]
    assert details == ["mail: npx mail-mcp", "db: http://localhost:9/mcp"]


def test_fix_removes_open_rules_and_adds_deny(settings_home, make_session):
    write_settings(settings_home / "settings.json", allow("Bash(ls)"))
    project = settings_home / "work"
    local = write_settings(
        project / ".claude" / "settings.local.json",
        allow("Bash(python3:*)", "Bash(ssh host *)", "Bash(git push *)",
              defaultMode="bypassPermissions"))
    make_session(make_id(1), [user_line("hi", cwd=str(project))])
    _, paths = claudes.posture_findings(claudes.load_sessions())
    plan = claudes.fix_plan(paths)

    _, user_new = plan[str(settings_home / "settings.json")]
    assert user_new["permissions"]["allow"] == ["Bash(ls)"]
    assert user_new["permissions"]["deny"] == claudes.DEFAULT_DENY

    _, local_new = plan[local]
    # Narrower risks stay: the fix only takes out what opens everything
    assert local_new["permissions"]["allow"] == ["Bash(git push *)"]
    assert "defaultMode" not in local_new["permissions"]


def test_fix_keeps_existing_deny_rules(settings_home):
    write_settings(settings_home / "settings.json",
                   {"permissions": {"deny": ["Read(~/.ssh/**)", "Mine"]}})
    _, paths = claudes.posture_findings([])
    _, new = claudes.fix_plan(paths)[str(settings_home / "settings.json")]
    deny = new["permissions"]["deny"]
    assert deny[:2] == ["Read(~/.ssh/**)", "Mine"]
    assert deny.count("Read(~/.ssh/**)") == 1


def test_fix_creates_user_settings(settings_home):
    plan = claudes.fix_plan([])
    old, new = plan[str(settings_home / "settings.json")]
    assert old == {}
    assert new["permissions"]["deny"] == claudes.DEFAULT_DENY


def test_fix_writes_backup(settings_home):
    path = write_settings(settings_home / "settings.json", allow("Bash"))
    claudes.write_settings(path, allow())
    with open(path + ".claudes-bak") as handle:
        assert json.load(handle) == allow("Bash")
    with open(path) as handle:
        assert json.load(handle) == allow()


def test_fix_leaves_managed_settings_alone(settings_home):
    managed = write_settings(settings_home / "managed.json", allow("Bash"))
    plan = claudes.fix_plan([managed])
    assert managed not in plan
