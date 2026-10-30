# Changelog

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
