# claudes

<p align="center">
  <img src="logos/claudes-logo.svg" alt="claudes" width="64" height="64" />
</p>

[![Tests](https://github.com/ridingbytes/claudes/actions/workflows/test.yml/badge.svg)](https://github.com/ridingbytes/claudes/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

*Where did I start that session?*

A command line tool to list, find, resume and audit your
[Claude Code](https://github.com/anthropics/claude-code) sessions across every
directory on your machine.

Claude Code keeps one transcript per session, filed under the
directory it was started in. After a few weeks there are
sessions in your home directory, in a dozen project folders,
some named, most not, some started from the Claude desktop app
and some from a terminal. `claude --resume` only shows the
sessions of the current directory. `claudes` shows all of them
in one list, tells you which ones are running right now, and
takes you back into any of them with one command.

It also reads the transcripts with a security eye: secrets that
ended up in a chat or a tool output, force pushes, `curl | sh`,
sessions that ran with permissions bypassed.

```
$ claudes
id          active  status     source    title                     directory            msgs
3f2a9c1e        2m  run:busy   term      api-refactor              ~/develop/api          84
b81e0d4a       40m  run:idle   app       Weekly mail digest        ~/Documents/inbox     186
71c4e2f0       18h             term      release 2.4               ~/develop/web          53
a09d33b7        3d             term      Fix flaky upload test     ~/develop/api          11
```

## What it does

- **One list for all sessions.** Newest first, with title,
  directory, number of prompts, and whether the session is open
  in a terminal right now (`run:idle`, `run:busy`) or running
  in the background (`bg`).
- **Filter and sort.** Free text query over title, path, branch
  and prompts. `--named` / `--unnamed`, `--app` / `--term`,
  `--dir` / `--under` / `--here`, `--older 30d` / `--newer 2h`,
  `--sort active|started|name|dir|msgs|size|cost`.
- **Act on many at once.** `-q` prints ids, `-` reads them back:
  `claudes --unnamed --older 30d -q | claudes rm -`. `rm` also
  takes the filters directly. `--format` and `--json` for
  scripts.
- **Resume from anywhere.** `claudes -r api-refactor` changes
  into the session's directory and runs `claude --resume`.
  A session that is already open in another terminal is not
  opened twice; `--fork` branches off a copy instead.
- **Pick with fzf.** `claudes pick` opens a fuzzy finder with a
  preview of each session; Enter resumes.
- **Manage.** Rename sessions (`claudes rename`), move them to
  the Trash (`claudes rm`), search the full text of every
  conversation (`claudes grep`).
- **Audit.** `claudes audit` scans transcripts for secrets and
  risky actions and prints masked findings by severity.
  `claudes review` starts a Claude Code `/security-review` in a
  session's repository.

Everything runs locally. `claudes` reads the files Claude Code
already writes under `~/.claude` and sends nothing anywhere.

## Install

Python 3.10 or newer, no dependencies. `fzf` is optional and only
needed for `claudes pick`.

```sh
pipx install git+https://github.com/ridingbytes/claudes
```

Or as a single file, since the whole tool is one script:

```sh
curl -fsSLo ~/bin/claudes \
  https://raw.githubusercontent.com/ridingbytes/claudes/master/claudes.py
chmod +x ~/bin/claudes
```

See [docs/installation.md](docs/installation.md) for a checkout
based setup and shell completion of session names.

## Quick start

```sh
claudes                       # all sessions, newest first
claudes api                   # sessions matching "api"
claudes --here                # sessions in or below this directory
claudes --unnamed -s msgs     # unnamed sessions, longest first
claudes pick                  # fuzzy finder, Enter resumes
claudes show api-refactor     # details, first and last prompt
claudes -r api-refactor       # resume it in its own directory
claudes rename 71c4 "release 2.4"
claudes grep "connection pool"
claudes audit                 # secrets and risky actions
claudes review api-refactor   # /security-review in that repo

# clean up: unnamed sessions started in ~, idle for a month
claudes --unnamed --dir ~ --older 30d       # look first
claudes rm --unnamed --dir ~ --older 30d    # then move to Trash
claudes --older 90d -q | claudes rm -       # or through a pipe
```

A session is addressed by an id prefix (`71c4`) or by its title.
When several sessions share a title, `show` and `resume` take
the most recent one; when a reference still fits several, a
picker opens with just those.

## Documentation

| Document | Contents |
|---|---|
| [docs/installation.md](docs/installation.md)       | pipx, single file, checkout, completion |
| [docs/usage.md](docs/usage.md)                     | Every command and option with examples |
| [docs/audit.md](docs/audit.md)                     | What `audit` detects, severities, `review` |
| [docs/how-it-works.md](docs/how-it-works.md)       | Transcript format, live registry, caches |
| [docs/security.md](docs/security.md)               | What is read and written, masking, privacy |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Missing sessions, slow scans, wrong titles |
| [docs/development.md](docs/development.md)         | Tests, code style, release steps |
| [CHANGELOG.md](CHANGELOG.md)                       | Release history |

## Caveats

`claudes` reads Claude Code's on-disk transcript format, which
is not a public interface and changes between releases. Fields
it does not find are shown as `?` rather than failing. Claude
Code deletes transcripts older than `cleanupPeriodDays` (30 by
default); those sessions cannot be listed anymore.

This project is not affiliated with or endorsed by Anthropic.

## License

[MIT](LICENSE), Copyright (c) 2026 RIDING BYTES GmbH.
