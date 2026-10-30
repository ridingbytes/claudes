# Installation

`claudes` is a single Python file without dependencies. It needs
Python 3.10 or newer and runs on macOS and Linux. `fzf` is
optional and only used by `claudes pick`.

## pipx

```sh
pipx install git+https://github.com/ridingbytes/claudes
```

Upgrade later with `pipx upgrade claudes`.

## Single file

```sh
curl -fsSLo ~/bin/claudes \
  https://raw.githubusercontent.com/ridingbytes/claudes/master/claudes.py
chmod +x ~/bin/claudes
```

Any directory on your `PATH` works instead of `~/bin`. Open a
new shell or run `rehash` (zsh) / `hash -r` (bash) so the shell
finds the new command.

## From a checkout

Useful when you want to change the tool and use the change
right away:

```sh
git clone git@github.com:ridingbytes/claudes.git ~/develop/claudes
ln -sf ~/develop/claudes/claudes.py ~/bin/claudes
```

## fzf

```sh
brew install fzf          # macOS
sudo apt install fzf      # Debian, Ubuntu
```

## Shell completion of session titles

There is no completion script yet. A small zsh function that
completes titles and id prefixes for the commands that take a
session:

```zsh
_claudes_sessions() {
  local -a items
  items=(${(f)"$(NO_COLOR=1 claudes -a | awk '{print $1}')"})
  _describe "session" items
}
compdef '_arguments "1: :(list pick show resume r rename rm grep audit review)" "2: :_claudes_sessions"' claudes
```

## Check the install

```sh
claudes --version
claudes -n 5
```

The first run parses every transcript and takes about a second
per gigabyte. Later runs read the cache and finish in a fraction
of a second. See [how-it-works.md](how-it-works.md).

## Uninstall

```sh
pipx uninstall claudes        # or: rm ~/bin/claudes
rm ~/.cache/claude-sessions.json ~/.cache/claude-sessions-audit.json
```

Sessions moved to the Trash with `claudes rm` stay in
`~/.Trash/claude-sessions/` until you empty the Trash.
