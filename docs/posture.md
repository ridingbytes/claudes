# Posture

`claudes audit` looks back at what happened. `claudes posture`
looks at what can happen next: which commands and tools Claude
Code may use without asking you, read from the settings files.

```sh
claudes posture                      # every finding, grouped by file
claudes posture --explain            # with the reason for each
claudes posture --min-severity high  # only what opens everything
claudes posture --json               # for scripts
claudes posture --fix                # remove the worst rules, add deny rules
```

## Which files are read

| File | Applies to |
|---|---|
| `/Library/Application Support/ClaudeCode/managed-settings.json`, `/etc/claude-code/managed-settings.json` | every session on the machine |
| `~/.claude/settings.json` | every session of yours |
| `<dir>/.claude/settings.json`, `<dir>/.claude/settings.local.json` | sessions started in `<dir>` |
| `~/.claude.json` | MCP servers, user wide and per project |

The project directories are the ones your sessions were started
in, plus your home directory. Allow lists from all files that
apply to a session are merged, so one open rule in a project file
is enough to open that project.

## Allow rules

| Category | Severity | Example | Why |
|---|---|---|---|
| runs anything            | high   | `Bash`, `Bash(*)`, `Bash(python3:*)`, `Bash(zsh:*)`, `Bash(node *)` | An interpreter or shell runs whatever it is given, so every command passes |
| remote shell             | high   | `Bash(ssh host *)`, `Bash(docker exec *)` | Any command on another machine or container, sudo included |
| runs anything (indirect) | medium | `Bash(find:*)`, `Bash(awk:*)`, `Bash(git *)` | `find -exec`, `awk system()`, `git -c` run other commands |
| pushes code              | medium | `Bash(git push *)` | Commits leave the machine unseen |
| installs software        | medium | `Bash(brew install *)`, `Bash(pip install *)` | Install scripts run with your rights |
| network tool             | medium | `Bash(curl *)` | Any file can be sent to any host |
| fetches any url          | medium | `WebFetch`, `WebFetch(domain:*)` | Fetched pages can carry instructions |
| every mcp tool           | medium | `mcp__server` | Sending, deleting and publishing tools included |
| reads any file           | low    | `Bash(cat:*)` | Covered by Read deny rules, so add them |

A rule is only flagged when it ends in a wildcard. `Bash(python
-m pytest:*)` or `Bash(ssh host uptime)` are narrow and pass.

## Settings

| Category | Severity | Trigger |
|---|---|---|
| bypass mode      | high   | `defaultMode: bypassPermissions` in user or managed settings |
| bypass mode      | medium | the same in a project file, which Claude Code 2.1.257 and later ignore |
| no deny rules    | medium | user and managed settings have no `permissions.deny` |
| sandbox off      | medium | no user or managed file sets `sandbox.enabled` |
| sandbox reads keys | low  | the sandbox is on but `filesystem.denyRead` lacks `~/.ssh` |
| project mcp auto | medium | `enableAllProjectMcpServers` is set |
| auto mode        | low    | `defaultMode: auto`: a classifier decides instead of you |
| mcp server       | low    | one line per configured MCP server |

claude.ai connectors such as mail or drive are configured in the
claude.ai settings, not in a local file, and do not show up.

## --fix

`--fix` makes the two changes that are safe to make blindly:

1. It removes the **runs anything** and **remote shell** allow
   rules and a `bypassPermissions` default mode from every user
   and project file. Claude Code then asks again for those
   commands, and "Yes, and don't ask again" can store a narrower
   rule.
2. It adds a default set of deny rules to
   `~/.claude/settings.json`: reads of `~/.ssh`, `~/.aws`,
   `~/.gnupg`, `~/.config/gh`, `~/.config/op`, `~/.netrc`, `.env`
   files, `*.pem` and `*.kdbx` under your home, and `sudo`,
   `git push --force` and `curl | sh`. Existing deny rules are
   kept.

The plan is printed as a diff of rules and applied after a
confirmation. Every changed file is backed up to
`<file>.claudes-bak` first. Managed settings are never changed.
Running sessions pick up the new rules without a restart.

The sandbox is not switched on by `--fix`: it blocks network
access except through an allow list, which breaks `ssh`, `git
push` and package installs until you add the hosts you use. Turn
it on by hand and test:

```json
{
  "sandbox": {
    "enabled": true,
    "filesystem": {"denyRead": ["~/.ssh", "~/.aws", "~/.gnupg"]}
  }
}
```

## What posture does not do

- Deny rules on Bash are prefix matches. `Bash(sudo *)` stops
  `sudo rm`, not `bash -c "sudo rm"`. Read deny rules cover `cat`
  and the like, not a script that opens the file itself. Only the
  sandbox enforces file and network limits for every process.
- It does not know which tools an MCP server offers, only that
  it is configured.
- It does not judge whether you need a rule. A wildcard for
  `ssh` to a test VM may be fine; on a production host it is not.
