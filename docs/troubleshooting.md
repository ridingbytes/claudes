# Troubleshooting

## A session is missing from the list

- Run `claudes -a`. Sessions without prompts and sessions that
  were continued under a new id are hidden by default.
- Search the full text: `claudes grep <something you said>`.
- Claude Code deletes transcripts whose last change is older
  than `cleanupPeriodDays` (default 30 days). Once the file is
  gone, the session cannot be listed or resumed.
- Sessions of the Claude desktop app only appear when they ran
  through Claude Code (the Code tab). Regular claude.ai chats
  are not stored locally.

## "session is open in another terminal"

The session is held by a running process. `claudes show <id>`
prints its pid and terminal. Switch to that terminal, or close
it, or start a branch with `claudes r <id> --fork`.

If the process is gone but the status still says running, the
pid was reused by another process. Delete the stale file
`~/.claude/sessions/<pid>.json`.

## "directory is gone"

The session was started in a directory that no longer exists.
Recreate it (a `git clone` to the same path is enough) and
resume again. The transcript path from `claudes show` is still
readable either way.

## Resuming opens the session picker instead of the session

`claude --resume <id>` looks for the session in the project
folder of the current directory. If the session was started in
a directory whose path changed (moved, renamed, symlinked),
resume from the old path or move the transcript to the matching
project folder under `~/.claude/projects/`.

## Wrong or ugly titles

Titles fall back to the first prompt when a session has no
name. Give it one with `claudes rename <id> <name>`, or
`/rename` inside the running session. `claudes --unnamed -s
msgs` lists the sessions most worth naming.

## The first run is slow

The first run parses every transcript, about one second per
gigabyte for `list` and about 20 seconds per gigabyte for
`audit`. Later runs use the caches. If every run is slow, check
that `~/.cache` is writable.

## Something looks stale

Delete the caches; they are rebuilt on the next run:

```sh
rm ~/.cache/claude-sessions.json ~/.cache/claude-sessions-audit.json
```

## After a Claude Code update, fields show as "?"

The transcript format is internal to Claude Code and changes
now and then. Please open an issue with the Claude Code version
(`claude --version`) and, if you can, a redacted sample line of
the record type that changed.

## Memories are gone after renaming a directory

Claude Code files memories by the path of the project directory,
so after a rename the new path starts without them. `claudes
memory --gone` lists the orphaned ones, and `claudes memory mv
--dir OLD --to NEW` moves them to the new path. See
[memory.md](memory.md#mv).
