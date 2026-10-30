# Usage

```
claudes [list] [QUERY...] [FILTERS]   list sessions
claudes pick [QUERY...] [FILTERS]     fzf picker, Enter resumes
claudes show ID                       details of one session
claudes resume ID [--fork]            resume in its directory
claudes rename ID NAME                set the session title
claudes rm ID...                      move sessions to Trash
claudes grep TEXT                     full text search
claudes audit [QUERY...] [FILTERS]    secrets and risky actions
claudes review ID|DIR [FOCUS...]      start /security-review there
```

`list` is the default command: `claudes api` is the same as
`claudes list api`. `ls`, `p` and `r` are short aliases for
`list`, `pick` and `resume`.

## Addressing a session

Every command that takes `ID` accepts:

- an id prefix, as short as it stays unique: `71c4`
- a title, matched without case: `api-refactor`
- a part of a title, if only one session contains it: `refac`

When a title is shared by several sessions, `show`, `resume` and
`review` pick the most recently active one. `rename` and `rm`
refuse and list the candidates instead, so you never change the
wrong session by accident.

## list

```sh
claudes                         # newest 'active' first
claudes api upload              # every term must match
claudes -n 10                   # only the first ten
```

Columns:

| Column | Meaning |
|---|---|
| id        | First 8 characters of the session id |
| active    | Time since the last message: `5m`, `3h`, `2d`, or a date |
| status    | `run:idle` / `run:busy` when a process holds the session open, `bg:` for background sessions |
| source    | `app` (Claude desktop app) or `term` (terminal); `term>app` means started in a terminal and continued in the app; `bg` marks background sessions |
| title     | Your name for the session (bold), an agent or AI generated title, or the first prompt (dimmed) |
| directory | Working directory the session was started in |
| msgs      | Number of prompts you typed |

The query matches title, directory, session id, git branch and
the first prompt. All terms must match.

### Filters

| Option | Effect |
|---|---|
| `-a`, `--all`     | Include sessions without prompts and sessions that were continued under a new id |
| `--here`          | Only sessions started in or below the current directory |
| `--named`         | Only sessions you gave a name (`/rename`, `claude -n`, `claudes rename`) |
| `--unnamed`       | Only sessions without such a name; AI generated titles count as unnamed |
| `--app`           | Only sessions started in the Claude desktop app |
| `--term`          | Only sessions started in a terminal |
| `-n N`            | Show at most N sessions |

Hidden by default: sessions without a single prompt, and
sessions that Claude Code continued in a new session after
compacting. The successor is listed; `-a` shows both. Running
sessions are always listed.

### Sorting

`-s KEY` with one of:

| Key | Order |
|---|---|
| `active`  | Last activity, newest first (default) |
| `started` | First message, newest first |
| `name`    | Title, A to Z |
| `dir`     | Directory, A to Z |
| `msgs`    | Number of prompts, most first |
| `size`    | Transcript size, largest first |
| `cost`    | Recorded API cost, highest first |

`-R` (`--reverse`) reverses the order.

```sh
claudes --unnamed -s msgs       # big unnamed sessions worth naming
claudes --app -s started -R     # app sessions, oldest first
claudes -s cost -n 5            # the five most expensive sessions
```

## pick

Opens the filtered list in `fzf` with a preview of the selected
session at the bottom. Enter resumes it, Esc quits. Takes the
same query and filters as `list`.

```sh
claudes pick
claudes pick --term api
```

## show

```sh
claudes show api-refactor
```

Prints id, title and where it came from, directory, source,
branch, start and last activity, number of prompts, transcript
size, cost, process details if running (pid, kind, status,
terminal), linked pull requests, the transcript path, and the
first and last prompt.

## resume

```sh
claudes resume api-refactor
claudes r 71c4
claudes r api-refactor --fork
claudes -r 71c4                 # same, as with claude -r
```

Changes into the session's directory and replaces itself with
`claude --resume <id>`. Claude Code files sessions by
directory, so resuming from the wrong directory would not find
the session; `claudes` takes care of that.

- A background session is opened with `claude attach <id>`.
- A session open in another terminal is not resumed a second
  time; the error names the pid and terminal. `--fork` starts a
  new session from a copy of the conversation instead.
- If the directory no longer exists, `resume` stops with an
  error. Recreate the directory or use `claudes show` to read
  the transcript path.

## rename

```sh
claudes rename 71c4 "release 2.4"
```

Appends a `custom-title` record to the transcript, which is what
Claude Code does on `/rename`. The new title shows up in
`claudes`, in `claude --resume` and in the desktop app. Running
sessions are not renamed; use `/rename` inside them.

## rm

```sh
claudes rm 71c4 a09d
claudes rm -y 71c4              # no confirmation prompt
```

Moves the transcript and its sidecar directory (subagent
transcripts, tool results) to `~/.Trash/claude-sessions/<project>/`.
Nothing is deleted; empty the Trash to free the space. Running
sessions are refused.

## grep

```sh
claudes grep connection pool
```

Searches the text of your prompts and Claude's answers in every
transcript, case insensitive, and prints up to three snippets
per session. Reads all transcripts on every call, so it takes a
few seconds on large histories.

## audit and review

See [audit.md](audit.md).

## Environment

| Variable | Effect |
|---|---|
| `NO_COLOR` | Disable colors; output is also plain when piped |
