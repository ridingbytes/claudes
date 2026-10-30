# Usage

```
claudes [list] [QUERY...] [FILTERS]   list sessions
claudes pick [QUERY...] [FILTERS]     fzf picker, Enter resumes
claudes -r [ID] [--fork]              resume, as with claude -r
claudes show ID...|-                  details of sessions
claudes rename ID NAME                set the session title
claudes rm [ID...|-] [FILTERS]        move sessions to the Trash
claudes grep TEXT [FILTERS]           full text search
claudes audit [QUERY...] [FILTERS]    secrets and risky actions
claudes review ID|DIR [FOCUS...]      start /security-review there
```

`list` is the default command: `claudes api` is the same as
`claudes list api`. `ls`, `p` and `r` are short aliases for
`list`, `pick` and `resume`. `claudes -h` lists the commands,
`claudes COMMAND -h` the options of one.

## Addressing a session

Every command that takes `ID` accepts:

- an id prefix, as short as it stays unique: `71c4`
- a title, matched without case: `api-refactor`
- a part of a title, if only one session contains it: `refac`
- `-` for ids on stdin, one per line (see [Pipes](#pipes))

When a title is shared by several sessions, `show`, `resume` and
`review` take the most recently active one. When a reference
still fits several sessions, `claudes` opens the fzf picker with
just those candidates; without a terminal or fzf it lists them
and stops. Ids from a pipe are matched by id only and never
open a picker, so a script cannot act on a session nobody
chose.

## list

```sh
claudes                         # most recently active first
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

Title and directory share the width of the terminal. On a
terminal a footer tells how many sessions are shown and how many
are hidden.

The query matches title, directory, session id, git branch and
the first and last prompt. All terms must match.

### Filters

These work for `list`, `pick`, `grep`, `audit` and `rm`.

| Option | Effect |
|---|---|
| `-a`, `--all`        | Include sessions without prompts and sessions that were continued under a new id |
| `--named`            | Only sessions you gave a name (`/rename`, `claude -n`, `claudes rename`) |
| `--unnamed`          | Only sessions without such a name; AI generated titles count as unnamed |
| `--app`              | Only sessions started in the Claude desktop app |
| `--term`             | Only sessions started in a terminal |
| `--dir PATH`         | Only sessions started exactly in PATH, for example `--dir ~` |
| `--under PATH`       | Only sessions started in PATH or below it |
| `--here`             | Same as `--under` with the current directory |
| `--older AGE`        | Idle for at least AGE: `30m`, `12h`, `30d`, `2w` |
| `--newer AGE`        | Active within the last AGE |
| `--from FILE`        | Only the session ids listed in FILE; `-` reads stdin |
| `-n N`               | Show at most N sessions |

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

### Output for scripts

| Option | Output |
|---|---|
| `-q`, `--ids`         | Full session ids, one per line |
| `--format TEMPLATE`   | One line per session from a template |
| `--json`              | A JSON array with all public fields |

Template fields: `id`, `short_id`, `title`, `title_source`,
`directory`, `branch`, `source`, `started`, `active`, `prompts`,
`cost`, `size`, `running`, `continued_in`, `prs`, `path`.

```sh
claudes --format "{short_id} {active} {directory}"
claudes --json | jq '.[] | select(.cost > 50) | .title'
```

## Pipes

A selection made with filters can be handed to another command.
`-q` prints ids, and commands read them with `-` (as an `ID`) or
`--from -` (as a filter):

```sh
# what would match: unnamed sessions started in ~, idle for a month
claudes --unnamed --dir ~ --older 30d

# move exactly those to the Trash
claudes --unnamed --dir ~ --older 30d -q | claudes rm -

# details of every session that touched the api repository
claudes --under ~/develop/api -q | claudes show -

# audit only the app sessions
claudes --app -q | claudes audit --from -

# anything else, with xargs
claudes --older 90d -q | xargs -n1 echo
```

Lines may also be whole table rows: the first word, the short
id, is what counts. That allows editing a list in between:

```sh
claudes --older 30d > old.txt     # delete the lines to keep
claudes rm - < old.txt
```

## pick

Opens the filtered list in `fzf` with a table header and a
preview of the selected session at the bottom. Enter resumes,
ctrl-y copies the session id (with `pbcopy`, `wl-copy` or
`xclip`), Esc quits. A query is handed to fzf as the initial
search, so you can still edit it there.

```sh
claudes pick
claudes pick --term api
```

## show

```sh
claudes show api-refactor
claudes show 71c4 a09d
claudes --older 30d -q | claudes show -
```

Prints id, title and where it came from, directory, source,
branch, start and last activity, number of prompts, transcript
size, cost, process details if running (pid, kind, status,
terminal), linked pull requests, the transcript path, the
command to resume it, and the first and last prompt.

## resume

```sh
claudes -r api-refactor
claudes -r 71c4 --fork
claudes -r                      # no id: pick one with fzf
claudes resume api-refactor     # long form; r is the alias
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
  error. Recreate the directory and resume again.

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
claudes rm 71c4 a09d                     # by id or title
claudes rm --unnamed --older 30d         # by filter
claudes rm --empty                       # every session without a prompt
claudes --dir ~ --older 60d -q | claudes rm -
claudes rm -y 71c4                       # no confirmation
```

Lists what it is about to move and asks first. The question goes
to the terminal even when the ids come from a pipe; without a
terminal, `-y` is required.

Moves each transcript and its sidecar directory (subagent
transcripts, tool results) to `~/.Trash/claude-sessions/<project>/`.
Nothing is deleted; empty the Trash to free the space.

Filters in `rm` also see hidden sessions, since an old continued
transcript is just as removable. Running sessions are refused
when you name them and skipped when a filter matches them.
Without ids, filters or `--empty`, `rm` does nothing.

## grep

```sh
claudes grep connection pool
claudes grep connection pool --term --older 7d
claudes grep deploy --max 10
```

Searches the text of your prompts and Claude's answers, case
insensitive, and prints up to `--max` snippets per session (3 by
default) with the match highlighted. Takes the same filters as
`list` and searches hidden sessions too. Reads the transcripts on
every call, so it takes a few seconds on large histories.

## audit and review

See [audit.md](audit.md).

## Environment

| Variable | Effect |
|---|---|
| `NO_COLOR`       | Disable colors; output is also plain when piped |
| `CLICOLOR_FORCE` | Keep colors when piped, for example into `less -R` |
