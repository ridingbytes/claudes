# Changelog

## 0.4.0

- `memory mv`: move memories to another project directory, one by
  one or with `--dir OLD` every memory of a renamed directory and
  its subprojects. Index lines move along; an identical copy in
  the target replaces the moved one; a different file of the same
  name stops the move.
- `memory --gone`: only memories whose project directory is gone.

## 0.3.0

- `claudes memory`: list the auto memories of every project with
  type and description, marking files missing from the project
  index, memories of directories that no longer exist, and the
  unused `~/.claude/memory` folder.
- `memory show`, `memory grep`, `memory edit`.
- `memory dupes`: memories kept in several projects.
- `memory promote`: append memories to `~/.claude/CLAUDE.md` as
  sections, with preview, confirmation and a backup. `--move`
  then removes the memory files and their index lines.
- `memory rm`: move memories to the Trash and unlink them.
- Project directories of sessions whose directory no longer has
  a transcript are recovered from the file system: `.`, `-` and
  umlauts that Claude Code encoded as `-` are matched against
  real names. Before, `~/.hermes` showed as `~//hermes`.
- A closed pipe (`claudes | head`) no longer prints a traceback
  at exit.

## 0.2.0

Review and refactoring of the whole tool, plus selecting many
sessions at once.

Selecting and pipes:

- Filters `--older AGE` and `--newer AGE` (`30m`, `12h`, `30d`,
  `2w`) for idle sessions, `--dir PATH` for sessions started
  exactly in a directory, `--under PATH` for a directory and
  everything below it.
- `-q` prints ids, `--format` fills a template, `--json` prints
  all fields.
- `-` as an id reads ids from stdin (`rm`, `show`), `--from FILE`
  restricts any filtered command to listed ids. Lines may be
  whole table rows.
- `rm` takes filters: `claudes rm --unnamed --dir ~ --older 30d`.
  It asks on the terminal even behind a pipe, and skips running
  sessions a filter matches.
- `rm --empty` moves every session without a prompt.
- `show` takes several sessions.

Easier to use:

- A reference that fits several sessions opens the fzf picker
  with just those, instead of stopping with a list.
- `claudes -r` without an id opens the picker, as `claude -r`
  does.
- `pick` has a table header, keeps colors, hands the query to
  fzf as an editable search, and ctrl-y copies the id. The key
  was advertised before but did nothing.
- Title and directory columns use the terminal width.
- `list` ends with a footer that counts hidden sessions.
- `grep` takes the list filters, highlights the match and has
  `--max`.
- `show` names where the title came from and prints the resume
  command.
- `claudes -h` shows examples; every option has help text.

Faster and more accurate:

- `audit` scans uncached transcripts in parallel: about three
  times faster on the first run.
- A private key counts only with a key body after the header.
  Text that merely mentions the header was reported before.

Internals:

- Sessions, metadata, findings and filters are dataclasses
  instead of loose dicts.
- One cache class serves both caches. The key is taken before a
  transcript is read, so a transcript that grows meanwhile is
  read again next time. Unchanged caches are not rewritten.
- The list of subcommands comes from the parser instead of a
  second hand-kept set.
- Ctrl-c exits with 130, a closed pipe quietly.
- Requires Python 3.10 (dataclass fields use `X | None`).

## 0.1.2

- `claudes -r ID` and `claudes --resume ID` resume a session,
  as `claude -r` does. Before, `-r` reversed the sort order of
  the list and the id was taken as a search term, so the
  command only printed the list.
- Reversing the sort order is now `-R` (`--reverse`).

## 0.1.1

- Write both caches with mode 0600 from the start. The metadata
  cache holds the first and last prompt of every session and was
  created world readable under the default umask.
- Require Python 3.10; 3.9 is end of life and no longer
  available on the CI runners.

## 0.1.0

First public release.

- List all Claude Code sessions across directories with title,
  directory, prompt count, last activity and live status.
- Filters: free text query, `--named` / `--unnamed`, `--app` /
  `--term`, `--here`, `--all`, `--limit`.
- Sorting by activity, start, name, directory, prompts, size or
  cost, with `--reverse`.
- `pick`: fzf picker with preview.
- `show`, `resume` (with `--fork`, `claude attach` for
  background sessions), `rename`, `rm` (to the Trash), `grep`.
- `audit`: masked secrets and risky actions by severity, with
  heredoc aware command matching and a cache that never stores
  raw secrets.
- `review`: start `/security-review` in a session's repository.
