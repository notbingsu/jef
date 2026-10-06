"""jev <request in plain words>. Jev picks the skill; browser skills drive a real Chrome tab, API skills don't."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, gcal, router, skills
from .agent import Agent
from .model import page_report

# api = "service.operation" in a leaf skill resolves here.
APIS = {"gcal": gcal.OPERATIONS}
# Below this routing confidence (use case × skill), ask before running the chosen skill.
SURE = 0.5


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def yes(prompt):
    # No terminal to ask means no.
    return sys.stdin.isatty() and input(prompt).strip().lower() in {"y", "yes"}


def approve(action, decision):
    return yes(f"{'':>9}  confirm {decision['operation']} “{action['label']}”? [y/N] ")


def confirm(title, lines):
    for line in lines:
        print(f"  {line}")
    if not sys.stdin.isatty():
        print("  no terminal to confirm in; nothing changed")
    return yes(f"  {title}? [y/N] ")


# What a low trace keeps of each decision: the choice and its cost, not the request that produced it.
DECISION = ("operation", "choice", "target", "confidence", "model", "usage", "latency_ms", "elapsed_ms")


def low_trace(trace):
    """The outcome and the steps: what ran, what was chosen, how long, at what cost. No page text, element tables,
    request bodies or raw answers, which are most of a full trace and all of the page content it holds."""
    kept = {k: v for k, v in trace.items() if k not in {"decision", "elements", "started_at"}}
    kept["skill"] = {k: trace["skill"][k] for k in ("path", "api", "url")}
    if trace.get("route"):
        kept["route"] = {k: v for k, v in trace["route"].items() if k != "answers"}
    if "page" in trace:
        kept["page"] = {k: trace["page"][k] for k in ("url", "title")}
    if "decisions" in trace:
        kept["decisions"] = [{k: d.get(k) for k in DECISION} for d in trace["decisions"]]
    if "judgments" in trace:
        kept["judgments"] = [{k: j.get(k) for k in ("model", "latency_ms", "usage")} for j in trace["judgments"]]
    return kept


def save_trace(skill, record, mode):
    """Write the run's trace under artifacts/runs/; returns its path, or None when tracing is off."""
    if mode == "off":
        return None
    trace = {"skill": skill.__dict__, **record}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path("artifacts", "runs", *skill.path.split("/"), f"{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(low_trace(trace) if mode == "low" else trace, indent=2, default=str))
    return path


def describe(step):
    target = f" [{step['target']}] {step['action']}" if step["target"] else ""
    text = f" ← “{step['text']}”" if step["text"] else ""
    effect = "  (no change)" if step["page_changed"] is False else ""
    return f"{step['elapsed_ms']:>6} ms  {step['operation']}{target}{text}  {step['probability']:.0%}{effect}"


def list_skills():
    for depth, path, description, _leaf in skills.outline():
        name = "  " * depth + path
        print(f"{name:<36} {description}" if description else name)


def pick(request):
    """Route the request; returns (Skill, routing record) or (None, record) when nothing should run."""
    path, route = router.route(request)
    if path is None:
        print(f"route: no skill fits ({route['use_case_probability']:.0%} sure). `jev --list` shows what exists.")
        return None, route
    sure = route["use_case_probability"] * route["skill_probability"]
    print(f"route: {path}  ({sure:.0%}, {route['latency_ms']} ms)", flush=True)
    if sure < SURE and not yes(f"  Not sure that's right. Run {path}? [y/N] "):
        print("  nothing ran.")
        return None, route
    return skills.load(path), route


def make_report(goal, skill, state):
    """What the finished page shows, for the trace and the CLI. A failure is recorded, never raised: it must not
    hide the run."""
    try:
        entries, missing, dropped, info = page_report(goal, state["page"], skill.rules)
    except (ValueError, RuntimeError) as error:
        return {"error": str(error)}
    return {"entries": entries, "missing": missing, "dropped": dropped, "model_call": info}


def print_report(report, mode):
    if "error" in report:
        print(f"  no report: {report['error']}")
        return
    for entry in report["entries"]:
        print(f"  {entry['name']:<28} {entry['when'] or '':<10} {entry['text'] or ''}")
    left_out = len(report["dropped"])
    if not report["entries"]:
        why = report["missing"] or "the page showed none of what you asked for."
        if left_out:
            why = f"{left_out} entries could not be copied exactly from the page."
        print(f"  Nothing to report: {why}")
    elif left_out:
        print(f"  ({left_out} entries had values left out: not found word for word on the page)")
    print(f"  (what the page showed when the run finished{'; the trace has the full text' if mode == 'full' else ''})")


def run_browser(skill, details, close=None, route=None, background=None, trace=None):
    goal = f"{skill.task}\n{details}" if details else skill.task
    mode = trace or config.get("trace")
    background = skill.background if background is None else background
    # A tab you never saw is clutter, so a background run cleans up after itself unless you ask it not to.
    close = background if close is None else close
    print(f"{skill.path} → {skill.url}{'  (background tab)' if background else ''}", flush=True)
    agent = Agent(skill.url, goal, rules=skill.rules, confirm=skill.confirm, approve=approve, background=background)
    shown, report = 0, None
    try:
        for state in agent.run():
            for step in state["history"][shown:]:
                print(describe(step), flush=True)
            shown = len(state["history"])
    finally:
        state = agent.snapshot()
        if skill.report and state["status"] == "done":
            report = make_report(goal, skill, state)
        path = save_trace(skill, {"route": route, **state, "report": report}, mode)
        if close:
            agent.close()
    outcome = {
        "done": "DONE: the model reports success."
        + (" Check the tab to confirm." if not close else " The trace has the page it saw." if mode == "full" else ""),
        "blocked": "BLOCKED: no supported action could make progress.",
        "declined": "STOPPED: a confirm action was not approved.",
    }[state["status"]]
    print(f"{state['elapsed_ms']:>6} ms  {outcome}")
    if path:
        print(f"{'':>9}  trace: {path}")
    if report:
        print_report(report, mode)
    return 0 if state["status"] == "done" else 1


def run_api(skill, details, route=None, trace=None):
    service, _, name = skill.api.partition(".")
    operation = APIS.get(service, {}).get(name)
    if operation is None:
        raise ValueError(f"{skill.path}: unknown api {skill.api!r}")
    print(f"{skill.path} → {skill.api}", flush=True)
    record = operation(skill, details, confirm)
    path = save_trace(skill, {"route": route, **record}, trace or config.get("trace"))
    outcome = "done" if record["status"] == "done" else "STOPPED: not confirmed; nothing changed."
    print(f"  {outcome}" + (f"\n  trace: {path}" if path else ""))
    return 0 if record["status"] == "done" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev", description=__doc__)
    parser.add_argument("request", nargs="*", help="what you want done, e.g. move the dentist to 5.30pm")
    parser.add_argument("--skill", help="skip routing and run this leaf, e.g. calendar/update-event")
    parser.add_argument("--list", action="store_true", help="show the skill tree")
    parser.add_argument("--route-only", action="store_true", help="show which skill Jev picks, then stop")
    parser.add_argument(
        "--close",
        action=argparse.BooleanOptionalAction,
        help="close a browser skill's tab afterwards; default: on for a background run, off for a visible one",
    )
    parser.add_argument(
        "--trace",
        choices=config.CHOICES["trace"],
        help="how much of this run to record in artifacts/runs/; default: jev.toml's trace (full if unset)",
    )
    parser.add_argument(
        "--background",
        action=argparse.BooleanOptionalAction,
        help="open the tab in the background instead of switching to it; default: the skill's own setting",
    )
    args = parser.parse_args(argv)
    request = " ".join(args.request).strip()
    load_environment()
    try:
        if args.list:
            list_skills()
            return 0
        if args.skill:
            skill, route = skills.load(args.skill), None
        elif not request:
            parser.error("say what you want done, e.g. jev move the dentist to 5.30pm")
        else:
            skill, route = pick(request)
            if skill is None:
                return 1
        if args.route_only:
            return 0
        if skill.api:
            return run_api(skill, request, route, args.trace)
        return run_browser(skill, request, args.close, route, args.background, args.trace)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"jev: {error}") from None
    except KeyboardInterrupt:
        raise SystemExit("jev: interrupted") from None


if __name__ == "__main__":
    sys.exit(main())
