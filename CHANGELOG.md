# Changelog

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
