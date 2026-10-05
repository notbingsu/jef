"""jev list | jev run <skill> [details...]. Watch the real Chrome tab; the terminal prints each action."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import skills
from .agent import Agent


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def approve(action, decision):
    # No terminal to ask means no approval.
    if not sys.stdin.isatty():
        return False
    reply = input(f"{'':>9}  confirm {decision['operation']} “{action['label']}”? [y/N] ")
    return reply.strip().lower() in {"y", "yes"}


def describe(step):
    target = f" [{step['target']}] {step['action']}" if step["target"] else ""
    text = f" ← “{step['text']}”" if step["text"] else ""
    effect = "  (no change)" if step["page_changed"] is False else ""
    return f"{step['elapsed_ms']:>6} ms  {step['operation']}{target}{text}  {step['probability']:.0%}{effect}"


def list_skills(_args):
    for depth, path, description, _leaf in skills.outline():
        name = "  " * depth + path
        print(f"{name:<36} {description}" if description else name)


def run(args):
    skill = skills.load(args.skill)
    details = " ".join(args.details).strip()
    goal = f"{skill.task}\n{details}" if details else skill.task
    print(f"{skill.path} → {skill.url}", flush=True)
    agent = Agent(skill.url, goal, rules=skill.rules, confirm=skill.confirm, approve=approve)
    shown = 0
    try:
        for state in agent.run():
            for step in state["history"][shown:]:
                print(describe(step), flush=True)
            shown = len(state["history"])
    finally:
        state = agent.snapshot()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trace = Path("artifacts", "runs", *skill.path.split("/"), f"{stamp}.json")
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(json.dumps({"skill": skill.__dict__, **state}, indent=2, default=str))
        if args.close:
            agent.close()
    outcome = {
        "done": "DONE: the model reports success. Check the tab to confirm.",
        "blocked": "BLOCKED: no supported action could make progress.",
        "declined": "STOPPED: a confirm action was not approved.",
    }[state["status"]]
    print(f"{state['elapsed_ms']:>6} ms  {outcome}\n{'':>9}  trace: {trace}")
    return 0 if state["status"] == "done" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the skill tree").set_defaults(handler=list_skills)
    runner = commands.add_parser("run", help="run one leaf skill")
    runner.add_argument("skill", help="leaf path, e.g. calendar/create-event")
    runner.add_argument("details", nargs="*", help="what this run should do, in plain words")
    runner.add_argument("--close", action="store_true", help="close the tab afterwards (default: leave it open)")
    runner.set_defaults(handler=run)
    args = parser.parse_args(argv)
    load_environment()
    try:
        return args.handler(args) or 0
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"jev: {error}") from None
    except KeyboardInterrupt:
        raise SystemExit("jev: interrupted") from None


if __name__ == "__main__":
    sys.exit(main())
