"""Auto memories: listing, finding duplicates, promoting to CLAUDE.md."""

import os

import pytest

import claudes
from conftest import make_id, user_line

NO_DASHES = """---
name: No dashes
description: Never use em-dashes in any text
metadata:
  type: feedback
---

Use commas or colons instead.

**Why:** reads as generated.
"""


def write_memory(claude_home, slug, stem, text, indexed=True):
    """Write a memory file and, if indexed, its index line."""
    directory = claude_home / "projects" / slug / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (stem + ".md")).write_text(text)
    if indexed:
        with open(directory / "MEMORY.md", "a") as handle:
            handle.write(f"- [{stem}]({stem}.md) -- note\n")
    return directory / (stem + ".md")


@pytest.fixture
def memories(claude_home, make_session, tmp_path):
    """Two projects sharing a memory, one unindexed, one user memory."""
    api = tmp_path / "work" / "api"
    web = tmp_path / "work" / "web"
    api.mkdir(parents=True)
    web.mkdir(parents=True)
    slug_api = claudes.slugify(str(api))
    slug_web = claudes.slugify(str(web))
    make_session(make_id(1), [user_line("x", cwd=str(api))], slug=slug_api)
    write_memory(claude_home, slug_api, "feedback_no_dashes", NO_DASHES)
    write_memory(claude_home, slug_web, "feedback_no_dashes", NO_DASHES)
    write_memory(claude_home, slug_web, "project_notes",
                 "---\nname: notes\ntype: project\n---\nweb notes\n",
                 indexed=False)
    user = claude_home / "memory"
    user.mkdir()
    (user / "reference_guides.md").write_text(
        "---\nname: guides\ntype: reference\n---\nguides live here\n")
    return {"api": api, "web": web, "slug_web": slug_web}


def run(capsys, argv):
    """Run claudes and return stdout."""
    claudes.main(argv)
    return capsys.readouterr().out


def test_frontmatter_with_nested_metadata():
    lines, body = claudes.split_frontmatter(NO_DASHES)
    fields = claudes.frontmatter_fields(lines)
    assert fields["name"] == "No dashes"
    assert fields["type"] == "feedback"
    assert body.startswith("Use commas")


def test_file_without_frontmatter():
    assert claudes.split_frontmatter("# Notes\ntext") == ([], "# Notes\ntext")


def test_decode_slug_recovers_dots_and_dashes(tmp_path):
    target = tmp_path / ".config" / "my-app"
    target.mkdir(parents=True)
    claudes.decode_slug.cache_clear()
    assert claudes.decode_slug(claudes.slugify(str(target))) == str(target)


def test_decode_slug_keeps_the_rest_of_a_gone_directory(tmp_path):
    claudes.decode_slug.cache_clear()
    slug = claudes.slugify(str(tmp_path)) + "-gone-dir"
    assert claudes.decode_slug(slug) == str(tmp_path / "gone-dir")


def test_memory_is_the_default_action(memories, capsys):
    out = run(capsys, ["memory"])
    assert out.count("feedback_no_dashes") == 2
    assert "not in index" in out
    assert "not loaded" in out
    assert run(capsys, ["mem", "guides"]).count("\n") == 1


def test_list_filters_by_type_and_directory(memories, capsys):
    out = run(capsys, ["memory", "--type", "project"])
    assert "project_notes" in out and "no_dashes" not in out
    out = run(capsys, ["memory", "--under", str(memories["api"])])
    assert out.count("\n") == 1


def test_dupes(memories, capsys):
    out = run(capsys, ["memory", "dupes"])
    assert "No dashes" in out
    assert out.count("feedback_no_dashes") == 2


def test_show_needs_a_unique_reference(memories, capsys):
    with pytest.raises(SystemExit):
        claudes.main(["memory", "show", "feedback_no_dashes"])
    assert "project/name" in capsys.readouterr().err
    out = run(capsys, ["memory", "show", "web/feedback_no_dashes"])
    assert "Use commas" in out


def test_promote_appends_section_and_backs_up(memories, claude_home,
                                              capsys):
    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_text("# Global\n\nBe brief.\n")
    run(capsys, ["memory", "promote", "-y", "api/feedback_no_dashes"])
    text = claude_md.read_text()
    assert text.startswith("# Global\n\nBe brief.\n\n## No dashes\n")
    assert "Use commas or colons instead." in text
    backup = claude_home / "CLAUDE.md.claudes-bak"
    assert backup.read_text() == "# Global\n\nBe brief.\n"


def test_promote_refuses_an_existing_section(memories, claude_home,
                                             capsys):
    (claude_home / "CLAUDE.md").write_text("## No dashes\n\nold\n")
    with pytest.raises(SystemExit):
        claudes.main(["memory", "promote", "-y",
                      "api/feedback_no_dashes"])
    assert "already has a section" in capsys.readouterr().err


def test_promote_move_removes_file_and_index_line(memories, claude_home,
                                                  capsys):
    run(capsys, ["memory", "promote", "-y", "--move",
                 "web/feedback_no_dashes"])
    directory = claude_home / "projects" / memories["slug_web"] / "memory"
    assert not (directory / "feedback_no_dashes.md").exists()
    assert "feedback_no_dashes" not in (directory / "MEMORY.md").read_text()
    trashed = claude_home / "trash" / "memory" / memories["slug_web"]
    assert (trashed / "feedback_no_dashes.md").exists()


def test_rm(memories, claude_home, capsys):
    run(capsys, ["memory", "rm", "-y", "guides"])
    assert not os.path.exists(claude_home / "memory"
                              / "reference_guides.md")


def test_grep(memories, capsys):
    out = run(capsys, ["memory", "grep", "commas"])
    assert out.count("feedback_no_dashes") == 2
    assert "Use commas" in out
