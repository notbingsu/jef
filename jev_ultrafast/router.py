"""A plain-language request → one leaf skill, in one TypeSafe request.

The use_case head chooses a top-level branch (or NONE). Each branch with several leaves gets its own skill head,
asked speculatively in the same request. Only the head for the chosen branch is consumed, as with operation and
target in the browser loop. Deeper branches are flattened into their top-level branch's head.
"""

import re
import time

from . import config, skills
from .model import typesafe, validate_choice, validate_noul
from .questions import FOLLOW_UP, FOLLOW_UP_CRITERIA, ROUTE, SKILL

NONE = "NONE"


def catalog(root=None):
    """{top-level name: {"description": ..., "skills": {leaf path: criterion}}} for every leaf in the tree."""
    entries = skills.outline(root)
    branches = {path.rstrip("/"): description for _, path, description, leaf in entries if not leaf}
    groups = {}
    for _, path, _, leaf in entries:
        if not leaf:
            continue
        skill = skills.load(path, root)
        parts = path.split("/")
        # Sub-branch descriptions ("in the browser (fallback)") help tell sibling skills apart.
        within = [branches["/".join(parts[:i])] for i in range(2, len(parts)) if branches.get("/".join(parts[:i]))]
        criterion = {
            "skill": path,
            "description": skill.description or skill.task,
            "task": skill.task,
            "kind": "api" if skill.api else "browser",
            **({"within": " · ".join(within)} if within else {}),
        }
        group = groups.setdefault(parts[0], {"description": branches.get(parts[0], ""), "skills": {}})
        group["skills"][path] = criterion
    return groups


def route(request, root=None, pending=None):
    """Returns (leaf path or None for NONE, routing record).

    With `pending`, a question a run stopped on, one more head asks whether this request is its answer. It rides
    in the same request as the routing heads, so it costs no extra round trip, and its probability is the
    record's `follow_up`. Whether to act on it is the caller's call, not the router's."""
    groups = catalog(root)
    if not groups:
        raise ValueError("There are no skills to choose from; add one under skills/.")
    use_cases = {
        name: {
            "use_case": group["description"] or name,
            "skills": [c["description"] for c in group["skills"].values()],
        }
        for name, group in groups.items()
    }
    use_cases[NONE] = "No skill can handle this request."
    questions = {
        "use_case": {"type": "choice", "criteria": use_cases, "instructions": {"request": request, "rules": ROUTE}}
    }
    heads = {}
    for name, group in groups.items():
        if len(group["skills"]) > 1:
            heads[name] = key = f"skill_in_{re.sub(r'\W', '_', name)}_{len(heads)}"
            questions[key] = {
                "type": "choice",
                "criteria": group["skills"],
                "instructions": {"request": request, "use_case": name, "rules": [ROUTE, SKILL]},
            }
    if pending:
        questions["follow_up"] = {
            "type": "noul",
            "criteria": FOLLOW_UP_CRITERIA,
            "instructions": {
                "question": FOLLOW_UP,
                "asked": pending["question"],
                "earlier_request": pending["earlier_request"],
            },
        }
    body = {
        "model": config.get("typesafe_model"),
        "state": {"request": request},
        "questions": questions,
    }
    started = time.perf_counter()
    result = typesafe(body)
    first = validate_choice(result["answers"].get("use_case", {}), use_cases)
    name = first["choice"]
    record = {
        "use_case": name,
        "use_case_probability": first["probabilities"][name],
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "answers": result["answers"],
    }
    if pending:
        # Recorded whether or not it wins, so a trace shows what the follow-up head thought either way.
        record["follow_up"] = round(validate_noul(result["answers"].get("follow_up", {})), 3)
    if name == NONE:
        return None, record
    if name in heads:
        # Unused heads cannot select anything. Validate only the head the chosen use case owns.
        second = validate_choice(result["answers"].get(heads[name], {}), groups[name]["skills"])
        path, probability = second["choice"], second["probabilities"][second["choice"]]
    else:
        path, probability = next(iter(groups[name]["skills"])), 1.0
    return path, {**record, "skill": path, "skill_probability": probability}
