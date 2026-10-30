# Audit

Claude Code transcripts hold everything that happened in a
session: your prompts, every command Claude ran, and the output
of those commands. That makes them a good record to check after
the fact, and a place where secrets end up in plain text.

`claudes audit` scans the transcripts for both. `claudes review`
starts a code security review in a session's repository.

## audit

```sh
claudes audit                        # all sessions, medium and up
claudes audit api --term             # same query and filters as list
claudes audit --only secret          # only secrets
claudes audit --only action          # only actions
claudes audit --min-severity low     # include low findings
claudes audit --min-severity high    # only the critical ones
claudes audit --max 50               # findings per session (default 15)
claudes --app -q | claudes audit --from -   # only piped sessions
```

Output per session: the list row, the permission modes the
session used, then one line per finding with severity,
category, where it was found, a repeat count, and a masked
detail. A summary line closes the report.

```
a09d33b7        3d             term      deploy scripts         ~/develop/ops          44
    modes  auto, bypassPermissions
    high   force push           assistant        …git log --oneline; git push --force origin master 2>&1 | tail -1
    high   permissions bypassed                  bypassPermissions mode
    high   github token         tool output      ghp_…dB (40)
    medium sudo                 assistant     x3 ssh host sudo systemctl restart app

53 sessions scanned: 4 high, 37 medium, 0 low, 1 distinct secrets
```

`audit` always includes empty and continued sessions, since a
secret in a superseded transcript is still on disk.

### Secrets

| Category | Severity | Pattern |
|---|---|---|
| private key         | high   | `-----BEGIN ... PRIVATE KEY-----` followed by a key body |
| anthropic key       | high   | `sk-ant-...` |
| openai key          | high   | `sk-...`, `sk-proj-...` |
| aws key             | high   | `AKIA...`, `ASIA...` |
| github token        | high   | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_` |
| slack token         | high   | `xoxb-`, `xoxp-`, ... |
| google key          | high   | `AIza...` |
| stripe key          | high   | `sk_live_`, `rk_live_` |
| wireguard key       | high   | `PrivateKey = ...` |
| jwt                 | medium | three base64url parts starting with `eyJ` |
| url credentials     | medium | `scheme://user:password@host` |
| password assignment | low    | `password=`, `secret:`, `api_key=`, ... with a literal value |

Values that look like code or placeholders are skipped:
`os.environ.get`, `${VAR}`, `changeme`, `example`, `xxx`,
dotted identifiers and the like. The password and URL patterns
still produce some noise, which is why the assignment category
is `low` and hidden by default.

Secrets are never printed in full. The detail shows the first
four and last two characters and the length, which is enough to
recognize a key you know and to look it up with
`claudes grep <prefix>`. Private keys show only their header.

Where a secret was found:

| Where | Meaning |
|---|---|
| prompt      | You typed or pasted it |
| tool output | A command printed it, for example `cat .env` |
| assistant   | Claude wrote it, usually inside a command or a file |

A secret in `tool output` was sent to the model as part of the
conversation. Treat it as exposed and rotate it.

### Actions

Bash commands Claude ran, checked against these patterns:

| Category | Severity | Example |
|---|---|---|
| pipe to shell        | high   | `curl ... \| sh` |
| force push           | high   | `git push --force`, `git push -f` |
| recursive delete     | high   | `rm -rf` on `/`, `~`, `$HOME`, `*`, a home directory |
| force push (lease)   | medium | `git push --force-with-lease` |
| database drop        | medium | `DROP DATABASE`, `DROP TABLE`, `dropdb` |
| disk tools           | medium | `mkfs`, `dd if=` |
| sudo                 | medium | `sudo ...` |
| keychain read        | medium | `security find-generic-password` |
| hard reset           | medium | `git reset --hard`, `git clean -f` |
| world writable       | medium | `chmod 777` |
| sandbox disabled     | medium | a Bash call with `dangerouslyDisableSandbox` |
| delete               | low    | any other `rm -r` |
| remote shell         | low    | `ssh`, `scp`, `rsync` |
| docker prune         | low    | `docker ... prune` |

Each command counts once, under the first pattern that matches.
Heredoc bodies (`cat > file <<EOF ... EOF`) are removed before
matching: they are file content, not commands.

Other tool calls:

| Category | Severity | Trigger |
|---|---|---|
| sensitive file read/edit/write | medium | `Read`, `Edit`, `Write` on `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `.env`, `*.pem`, `*.kdbx`, `id_rsa`, `credentials`, ... |
| outward tool         | low    | MCP tools named like send, reply, forward, publish, deploy, delete, share, execute |
| permissions bypassed | high   | The session ran in `bypassPermissions` mode |

Repeated identical findings in one session are counted, not
repeated (`x4`).

### Speed and cache

The first audit reads every transcript, in parallel on all
cores, and takes roughly 20 seconds per gigabyte on a laptop. Results are cached per transcript in
`~/.cache/claude-sessions-audit.json`, keyed by modification
time and size, so later runs only rescan changed sessions. The
cache holds masked values and short SHA-256 fingerprints, never
the secrets themselves, and is written with mode `0600`.

## review

```sh
claudes review api-refactor
claudes review .
claudes review ~/develop/api "focus on the upload endpoint"
```

Starts a new interactive Claude Code session that runs
`/security-review` on the current branch of a repository. The
argument is a session (its directory is used) or a directory.
Extra words are passed to the review as instructions.

The new session is named `security-review <title>`, so it is
easy to find later with `claudes --named review`. The directory
must be a git work tree; `/security-review` reviews the
pending changes of the branch.

## What audit does not do

- It does not judge intent. A force push on your own feature
  branch is routine; it is listed so you can decide.
- It does not see what happened outside Claude Code, or in
  transcripts Claude Code has already deleted.
- It is pattern based. A secret in an unusual format, or a
  destructive command written in an unusual way, is missed.
