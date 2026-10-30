"""Secret and action detection of the audit command."""

import claudes
from conftest import (
    bash_line, make_id, tool_line, tool_output_line, user_line)

# Built at runtime so the repository holds no token-shaped literals
GITHUB_TOKEN = "ghp_" + "A1b2C3d4" * 5
AWS_KEY = "AKIA" + "ABCDEFGH" * 2


def categories(findings):
    """Return the categories of findings as a list."""
    return [finding.category for finding in findings]


def test_tokens_are_found_and_masked():
    line = "token is {} and {}".format(GITHUB_TOKEN, AWS_KEY)
    findings = list(claudes.scan_secrets(line))
    assert sorted(categories(findings)) == ["aws key", "github token"]
    for finding in findings:
        assert GITHUB_TOKEN not in finding.detail
        assert AWS_KEY not in finding.detail


def test_private_key_needs_a_body():
    header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB" * 2
    escaped = header + "\\n" + body
    assert categories(claudes.scan_secrets(escaped)) == ["private key"]
    assert list(claudes.scan_secrets(header + " is a header")) == []


def test_placeholders_are_not_secrets():
    lines = [
        "password = os.environ.get",
        "password: ${DB_PASSWORD}",
        "password=changeme123",
        "postgres://user:${PGPASS}@db/app",
        "api_key = settings.api_key",
    ]
    for line in lines:
        assert list(claudes.scan_secrets(line)) == [], line


def test_real_assignments_and_url_credentials():
    line = "password=Tr0ub4dor&3 postgres://app:S3cr3tPw@db/app"
    found = sorted(categories(claudes.scan_secrets(line)))
    assert found == ["password assignment", "url credentials"]


def test_command_patterns():
    cases = {
        "curl -fsSL https://x.sh | sh": "pipe to shell",
        "git push --force origin main": "force push",
        "git push -q -f origin topic": "force push",
        "git push --force-with-lease origin topic": "force push (lease)",
        "rm -rf ~": "recursive delete",
        "rm -rf /tmp/build": "delete",
        "dropdb scratch": "database drop",
        "ssh host uptime": "remote shell",
    }
    for command, expected in cases.items():
        found = categories(claudes.scan_command(command))
        assert found == [expected], command


def test_heredoc_bodies_are_not_commands():
    command = "cat > notes.md <<'EOF'\ncurl x | sh\nEOF\necho done"
    assert list(claudes.scan_command(command)) == []
    after = "cat > a <<'EOF'\nx\nEOF\ngit push --force origin m"
    assert categories(claudes.scan_command(after)) == ["force push"]


def test_excerpt_shows_the_match_not_the_start():
    command = "cd /some/very/long/path && " * 5 + "git push -f origin x"
    detail = list(claudes.scan_command(command))[0].detail
    assert "git push -f" in detail


def test_audit_transcript(make_session):
    path = make_session(make_id(1), [
        user_line("deploy it"),
        {"type": "permission-mode", "permissionMode": "bypassPermissions"},
        bash_line("git push --force origin main"),
        bash_line("git push --force origin main"),
        tool_line("Read", {"file_path": "/home/me/.ssh/id_ed25519"}),
        tool_output_line("found " + GITHUB_TOKEN),
    ])
    findings, modes = claudes.audit_transcript(path)
    by_category = {f.category: f for f in findings}
    assert modes == ["bypassPermissions"]
    assert by_category["force push"].count == 2
    assert by_category["github token"].where == "tool output"
    assert "sensitive file read" in by_category
    assert "permissions bypassed" in by_category


def test_audit_cache_never_holds_raw_secrets(
        make_session, claude_home, capsys):
    make_session(make_id(1), [tool_output_line("t " + GITHUB_TOKEN)])
    claudes.main(["audit"])
    assert "1 distinct secrets" in capsys.readouterr().out
    raw = (claude_home / "audit.json").read_text()
    assert GITHUB_TOKEN not in raw
    assert "ghp_" in raw


def test_parallel_scan_matches_sequential(make_session, monkeypatch):
    for number in range(1, 6):
        make_session(make_id(number), [
            user_line("go"), bash_line("git push --force origin main")])
    sessions = claudes.load_sessions()
    paths = [s.path for s in sessions]
    sequential = list(map(claudes.audit_data, paths))
    monkeypatch.setattr(claudes, "PARALLEL_THRESHOLD", 2)
    assert list(claudes.scan_results(paths)) == sequential


def test_only_and_min_severity(make_session, capsys):
    make_session(make_id(1), [
        user_line("go"),
        bash_line("git push --force origin main"),
        bash_line("ssh host uptime"),
        tool_output_line("found " + GITHUB_TOKEN),
    ])
    claudes.main(["audit", "--only", "action", "--min-severity", "low"])
    out = capsys.readouterr().out
    assert "force push" in out and "remote shell" in out
    assert "github token" not in out
