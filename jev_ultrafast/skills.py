"""A tree of personal skills. Directories are branches, TOML files are leaves.

skills/
  _branch.toml            optional, applies to everything
  calendar/
    _branch.toml          url, rules and confirm shared by calendar leaves
    create-event.toml     task (required), description, and its own url/rules/confirm

A leaf inherits from every _branch.toml on its path: the deepest url wins, rules and confirm accumulate.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

BRANCH = "_branch.toml"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
BRANCH_KEYS = {"description", "url", "rules", "confirm"}
LEAF_KEYS = BRANCH_KEYS | {"task"}


@dataclass(frozen=True)
class Skill:
    path: str
    task: str
    url: str
    description: str = ""
    rules: tuple[str, ...] = ()
    confirm: tuple[str, ...] = ()


def default_root():
    return Path(os.environ.get("JEV_SKILLS_DIR", Path.cwd() / "skills"))


def read(file, keys):
    try:
        data = tomllib.loads(file.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file}: {error}") from None
    if unknown := set(data) - keys:
        raise ValueError(f"{file}: unknown keys {sorted(unknown)}; allowed {sorted(keys)}")
    for key in ("description", "url", "task"):
        if not isinstance(data.get(key, ""), str):
            raise ValueError(f"{file}: {key} must be a string")
    for key in ("rules", "confirm"):
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"{file}: {key} must be a list of non-empty strings")
    return data


def load(path, root=None):
    root = Path(root or default_root())
    parts = path.strip("/").removesuffix(".toml").split("/")
    if not all(NAME.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid skill path {path!r}; use lowercase names like calendar/create-event")
    leaf = root.joinpath(*parts).with_suffix(".toml")
    if not leaf.is_file():
        raise ValueError(f"No skill at {path!r} under {root}. Run `jev list`.")
    branches = [root.joinpath(*parts[:depth], BRANCH) for depth in range(len(parts))]
    layers = [read(f, BRANCH_KEYS) for f in branches if f.is_file()]
    data = read(leaf, LEAF_KEYS)
    url, rules, confirm = None, [], []
    for layer in [*layers, data]:
        url = layer.get("url", url)
        rules += layer.get("rules", [])
        confirm += layer.get("confirm", [])
    if not data.get("task", "").strip():
        raise ValueError(f"{leaf}: a leaf skill needs a task")
    if not url or not url.startswith(("https://", "http://")):
        raise ValueError(f"{leaf}: no http(s) url on this skill or its branches")
    return Skill(
        path="/".join(parts),
        task=data["task"].strip(),
        url=url,
        description=data.get("description", ""),
        rules=tuple(r.strip() for r in rules),
        confirm=tuple(dict.fromkeys(c.strip() for c in confirm)),
    )


def outline(root=None):
    """(depth, path, description, is_leaf) for every branch and leaf, depth-first and sorted."""
    root = Path(root or default_root())

    def walk(folder, depth):
        for entry in sorted(folder.iterdir()):
            path = entry.relative_to(root).with_suffix("").as_posix()
            if entry.is_dir() and NAME.fullmatch(entry.name):
                branch = entry / BRANCH
                description = read(branch, BRANCH_KEYS).get("description", "") if branch.is_file() else ""
                yield depth, path + "/", description, False
                yield from walk(entry, depth + 1)
            elif entry.suffix == ".toml" and entry.name != BRANCH and NAME.fullmatch(entry.stem):
                yield depth, path, read(entry, LEAF_KEYS).get("description", ""), True

    if not root.is_dir():
        raise ValueError(f"No skills directory at {root}")
    return list(walk(root, 0))
