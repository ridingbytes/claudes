# Security and privacy

## Data flow

`claudes` runs locally and makes no network requests. It reads:

- `~/.claude/projects/**/*.jsonl` (session transcripts)
- `~/.claude/sessions/*.json` (registry of running sessions)
- the process table, through `ps` and `kill -0`

It writes:

- `~/.cache/claude-sessions.json` (mode `0600`)
- `~/.cache/claude-sessions-audit.json` (mode `0600`)
- a `custom-title` record into a transcript on `rename`
- `~/.Trash/claude-sessions/` on `rm`

`resume` and `review` hand over to the `claude` binary with
`exec`; from then on Claude Code's own behavior and settings
apply.

## What the caches contain

The metadata cache holds, per transcript: directory, branch,
timestamps, titles, prompt count, cost, linked pull request
URLs, and the first and last prompt truncated to 500
characters. That is a copy of text that already sits in the
transcripts, but in a second place. If a prompt contained
something sensitive, it is in the cache as well; delete
`~/.cache/claude-sessions.json` after cleaning up a transcript.

The audit cache never holds a secret. It stores the masked form
(`ghp_…dB (40)`) and the first 12 hex characters of the value's
SHA-256, used to count distinct secrets across sessions. For
action findings it stores up to 160 characters of the command
around the match.

## Transcripts are sensitive

This is not specific to `claudes`, but the audit makes it
visible: Claude Code transcripts contain the full output of
every command Claude ran. A `cat .env`, a `docker inspect`, or a
failing `curl` with an auth header puts credentials into a file
in your home directory, and into the conversation that was sent
to the model.

When `claudes audit` reports a secret:

1. Rotate it. A secret in `tool output` or `prompt` was part of
   the model context.
2. Remove the session with `claudes rm`, or edit the transcript
   if you need to keep the rest of it.
3. Empty the Trash and delete both caches.
4. Check backups that include `~/.claude`.

Claude Code deletes transcripts older than `cleanupPeriodDays`
(setting in `~/.claude/settings.json`, default 30). A shorter
period limits how long secrets stay on disk.

## Reporting a vulnerability

Please report security issues privately through GitHub:
Security tab of the repository, "Report a vulnerability".
Do not open a public issue for them.
