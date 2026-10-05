"""Skill tree loading. Offline; no browser or model calls."""

from pathlib import Path

import pytest

from jev_ultrafast import skills


def write(root, path, text):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


@pytest.fixture
def tree(tmp_path):
    write(tmp_path, "_branch.toml", 'rules = ["Never invent personal details."]\n')
    write(tmp_path, "mail/_branch.toml", 'description = "Mail"\nurl = "https://mail.test/"\nconfirm = ["Send"]\n')
    write(tmp_path, "mail/drafts/_branch.toml", 'url = "https://mail.test/drafts"\nrules = ["Keep drafts short."]\n')
    write(
        tmp_path,
        "mail/drafts/reply.toml",
        'description = "Reply"\ntask = "Reply to the named thread."\nconfirm = ["Send", "Archive"]\n',
    )
    return tmp_path


def test_leaf_inherits_deepest_url_and_accumulates_rules_and_confirm(tree):
    skill = skills.load("mail/drafts/reply", tree)
    assert skill.url == "https://mail.test/drafts"
    assert skill.rules == ("Never invent personal details.", "Keep drafts short.")
    assert skill.confirm == ("Send", "Archive")
    assert skill.task == "Reply to the named thread." and skill.description == "Reply"


def test_outline_lists_branches_before_their_leaves(tree):
    assert [(d, p, leaf) for d, p, _, leaf in skills.outline(tree)] == [
        (0, "mail/", False),
        (1, "mail/drafts/", False),
        (2, "mail/drafts/reply", True),
    ]


@pytest.mark.parametrize(
    "path, content, message",
    [
        ("../escape", None, "Invalid skill path"),
        ("mail/missing", None, "No skill"),
        ("mail/typo", 'task = "x"\nrule = ["oops"]\n', "unknown keys"),
        ("mail/no-task", 'description = "x"\n', "needs a task"),
        ("mail/bad-rules", 'task = "x"\nrules = "not a list"\n', "list of non-empty strings"),
        ("other/no-url", 'task = "x"\n', "no http"),
    ],
)
def test_invalid_skills_are_rejected(tree, path, content, message):
    if content is not None:
        write(tree, path + ".toml", content)
    with pytest.raises(ValueError, match=message):
        skills.load(path, tree)


def test_repository_skills_load():
    root = Path(__file__).parent.parent / "skills"
    leaves = [path for _, path, _, leaf in skills.outline(root) if leaf]
    assert leaves
    for path in leaves:
        assert skills.load(path, root).url.startswith("https://")
