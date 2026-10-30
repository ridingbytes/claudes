# How it works

`claudes` has no state of its own apart from two caches. Every
answer comes from files Claude Code already writes.

## Transcripts

Claude Code stores one JSON Lines file per session:

```
~/.claude/projects/<project-slug>/<session-id>.jsonl
~/.claude/projects/<project-slug>/<session-id>/     sidecar: subagents, tool results
```

The project slug is the working directory with every `/` and
`.` replaced by `-`, so `/Users/me/develop/api` becomes
`-Users-me-develop-api`. The slug cannot be decoded reliably,
which is why `claudes` takes the directory from the `cwd` field
of the first record that has one.

Each line is one record. The ones `claudes` uses:

| Record | Used for |
|---|---|
| `user` with text content           | Prompt count, first and last prompt |
| any record with `timestamp`        | First and last activity |
| `cwd`, `gitBranch`, `entrypoint`   | Directory, branch, app or terminal |
| `sessionKind: "bg"`                | Background sessions |
| `custom-title`                     | The name you gave (`/rename`, `claude -n`) |
| `agent-name`                       | Name used by remote and background sessions |
| `ai-title`                         | Title Claude Code generated |
| `continued-in`                     | Session was continued under a new id |
| `cost-state`                       | Accumulated API cost |
| `pr-link`                          | Pull requests created in the session |
| `permission-mode`                  | Modes the session ran in (audit) |
| `assistant` with `tool_use`        | Commands and file access (audit) |

User records that only carry tool results, meta records and
compaction summaries do not count as prompts. Slash commands
count, with the command name as text.

### Title

The first available of: your name (`custom-title`), the agent
name, the AI title, the first prompt. The list renders your own
names bold and prompts dimmed, so it is visible at a glance
which sessions still need a name.

### Source

`entrypoint` is `cli` for the terminal and `claude-desktop` for
the desktop app. A session that was resumed in the other
environment carries both, in the order they appeared, shown as
`term>app` or `app>term`. `--app` and `--term` filter on where
the session was started.

## Running sessions

Every running Claude Code process registers itself in
`~/.claude/sessions/<pid>.json` with its session id, working
directory, kind (`interactive` or `bg`), status and name.
`claudes` reads these files and keeps only entries whose
process still exists. This is how the `status` column and the
guard against resuming an open session work. `ps` provides the
terminal device for `show`.

## Caches

| File | Contents |
|---|---|
| `~/.cache/claude-sessions.json`       | Parsed metadata per transcript |
| `~/.cache/claude-sessions-audit.json` | Masked audit findings per transcript |

Both are keyed by transcript path, modification time and size.
A changed transcript is parsed again; deleted transcripts drop
out of the cache. A version number in each file invalidates it
when the format changes. Deleting either file is always safe.

Parsing is line based. Most lines are skipped by a cheap string
check before any JSON decoding: large API request blobs, file
history snapshots and tool results never reach the JSON parser
when listing. This keeps the first scan of a gigabyte of
transcripts around one second.

## Writing

`claudes` writes to Claude Code's files in exactly two cases:

- `rename` appends one `custom-title` record to a transcript.
- `rm` moves a transcript and its sidecar to the Trash.

Both refuse to touch a running session, since Claude Code keeps
the transcript open and appends to it.
