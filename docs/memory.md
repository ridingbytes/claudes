# Memory

Claude Code writes auto memories: short Markdown notes about you,
your preferences and your projects, kept per project. After a
while there are hundreds of them, spread over every directory
you ever started a session in, and some of them are rules that
should hold everywhere but only exist in one project.

`claudes memory` lists them all, finds the ones kept in several
places, and promotes a memory to `~/.claude/CLAUDE.md`, which
every session loads.

```
claudes memory [QUERY...] [--type T] [--under PATH|--here] [--gone]
claudes memory show MEMORY
claudes memory grep TEXT
claudes memory dupes
claudes memory promote MEMORY... [--move] [-y]
claudes memory mv MEMORY...|--dir OLD --to DIR [-y]
claudes memory rm MEMORY... [-y]
claudes memory edit MEMORY
```

`mem` is short for `memory`, and `list` is its default action.

## Where memories live

| Location | Loaded |
|---|---|
| `~/.claude/projects/<project>/memory/*.md` | In sessions of that project, through the project's `MEMORY.md` index |
| `~/.claude/CLAUDE.md`                      | In every session |
| `~/.claude/memory/*.md`                    | Not loaded by Claude Code; listed as `not loaded` |

Each memory file starts with a frontmatter (`name`,
`description`, `type`), followed by the note itself. The type is
one of `user`, `feedback`, `project` or `reference`. A project's
`MEMORY.md` links its memory files; only that index is loaded at
the start of a session, and Claude reads the linked files when
they are relevant.

## Addressing a memory

`MEMORY` is a file name without `.md` (`feedback_no_dashes`), the
`name` from the frontmatter, or `project/file` as the list prints
it (`buildout/feedback_prs`). A reference that fits several
memories is refused with the candidates listed; add the project
to make it unique.

## list

```sh
claudes memory                          # every memory
claudes memory deploy                   # all words must match
claudes memory --type feedback          # only one type
claudes memory --under ~/develop/api    # only projects below a path
claudes mem --here
claudes memory --gone                   # orphaned memories only
```

Columns: type, project and file, description. Markers at the
end of a line:

| Marker | Meaning |
|---|---|
| `not in index` | The file exists but the project's `MEMORY.md` does not link it, so sessions do not see it |
| `project gone` | The project directory no longer exists, for example after a rename; the memory is orphaned |
| `not loaded`   | The file lies in `~/.claude/memory`, which Claude Code does not read |

Project directories come from the sessions of that project. When
there are none left, the project folder name is matched against
the file system, since Claude Code encodes `/`, `.` and non
ASCII letters all as `-`.

## show, grep, edit

```sh
claudes memory show buildout/feedback_prs
claudes memory grep translation
claudes memory edit feedback_no_dashes    # $VISUAL, $EDITOR, or vi
```

## dupes

```sh
claudes memory dupes
```

Lists memories with the same name in more than one project. A
rule you had to teach in several projects is a candidate for
`~/.claude/CLAUDE.md`. Duplicates with one copy marked `project
gone` are usually left over from a renamed directory; remove the
orphan with `claudes memory rm`.

## promote

```sh
claudes memory promote buildout/feedback_no_dashes
claudes memory promote api/feedback_no_dashes --move
```

Appends each memory to `~/.claude/CLAUDE.md` as a section:

```markdown
## No dashes

Never use em-dashes in any text

Use commas or colons instead.
```

The heading is the memory's name, followed by its description
and body. `promote` prints the text first and asks before
writing. It refuses when `CLAUDE.md` already has a heading with
that name. The previous `CLAUDE.md` is kept as
`~/.claude/CLAUDE.md.claudes-bak`.

With `--move`, the memory files are moved to the Trash
afterwards and their lines removed from the project indexes, so
the rule lives in one place. Remove the other copies that
`dupes` showed the same way:

```sh
claudes memory promote api/feedback_no_dashes --move
claudes memory rm web/feedback_no_dashes
```

Edit `CLAUDE.md` afterwards if the section should read like the
rest of the file: a memory is written as a note about a
situation, `CLAUDE.md` as standing instructions.

## mv

```sh
claudes memory mv --dir ~/develop/api --to ~/work/api
claudes memory mv api/project_plan --to ~/work/api-v2
```

Claude Code finds a project's memories by the path of its
directory. After a directory was moved or renamed, its memories
are no longer loaded, and the list marks them `project gone`.
`mv` moves them to the memory folder of the new path.

With `--dir OLD`, every memory of OLD and of the projects below it
moves, and the part below OLD is kept: memories of `OLD/app` go to
`DIR/app`. Without it, the named memories go to DIR itself. DIR
must exist.

The line that links a memory from its old `MEMORY.md` moves to the
new one. A memory that was not in the index stays out of it. When
the target already has a file of the same name and text, the moved
copy goes to the Trash; when the text differs, nothing is moved
until one of the two is merged or removed. `mv` prints the plan
and asks first.

A directory that no longer exists cannot always be told from its
folder name: `old-app` and `old/app` encode the same. A subproject
counts as below OLD only when one of its sessions recorded the
directory. Move the others by name.

## rm

```sh
claudes memory rm web/feedback_no_dashes
```

Moves the file to `~/.Trash/claude-sessions/memory/<project>/`
and removes its line from the project's `MEMORY.md`.
