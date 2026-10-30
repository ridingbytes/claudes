# Development

## Setup

```sh
git clone git@github.com:ridingbytes/claudes.git
cd claudes
python3 -m venv .venv && . .venv/bin/activate   # or a pyenv virtualenv
pip install -e ".[dev]"
```

## Tests and lint

```sh
pytest -q
flake8
```

CI runs both on every push and pull request against `master`
(`.github/workflows/test.yml`).

The tests build a fake `~/.claude` tree in a temporary
directory (`tests/conftest.py`) and point the module constants
at it, so they never touch your real sessions. Token-shaped
test values are assembled at runtime, which keeps literal
secrets out of the repository and out of GitHub's push
protection.

## Code style

- One file, standard library only. Keep it that way so the
  single file install keeps working.
- flake8 clean with a line length of 80, double quotes.
- Small functions that do one thing and operate on a single
  item (`read_meta(path)`, `scan_command(command)`); callers
  iterate.
- No blind `except Exception`. Catch the errors you expect.
- Parsing must tolerate unknown records and missing fields.
  The transcript format is not a public interface.

## Changing the transcript parser

When a field is added to the parsed metadata, bump
`CACHE_VERSION`. When audit patterns or the finding format
change, bump `AUDIT_VERSION`. Both invalidate the respective
cache on the next run.

## Adding an audit pattern

Add a tuple to `SECRET_PATTERNS` or `COMMAND_PATTERNS`, add a
case to `tests/test_audit.py`, bump `AUDIT_VERSION`, and add a
row to the tables in [audit.md](audit.md). Run the audit over
real transcripts before committing and check the new pattern
for false positives.

## Release

1. Update `__version__` in `claudes.py` and `version` in
   `pyproject.toml`.
2. Add a section to `CHANGELOG.md`.
3. Commit, tag `vX.Y.Z`, push the tag, create a GitHub release.
