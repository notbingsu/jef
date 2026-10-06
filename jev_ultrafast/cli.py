"""Running one request: route it, run the skill, report and trace it. `jev serve` calls execute() for each request it
receives; `jev --no-server` calls it in-process. Everything said or asked goes through the current console."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import browser, config, console, gcal, router, skills
from .agent import Agent
from .command import load_environment, parse
from .model import page_report

# api = "service.operation" in a leaf skill resolves here.
APIS = {"gcal": gcal.OPERATIONS}
# Below this routing confidence (use case × skill), ask before running the chosen skill.
SURE = 0.5


def approve(action, decision):
    return console.ask(f"{'':>9}  confirm {decision['operation']} “{action['label']}”? [y/N] ")


def confirm(title, lines):
    for line in lines:
        console.say(f"  {line}")
    if not console.current().interactive:
        console.say("  no terminal to confirm in; nothing changed")
    return console.ask(f"  {title}? [y/N] ")


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
        console.say(f"{name:<36} {description}" if description else name)


def pick(request):
    """Route the request; returns (Skill, routing record) or (None, record) when nothing should run."""
    path, route = router.route(request)
    if path is None:
        console.say(f"route: no skill fits ({route['use_case_probability']:.0%} sure). `jev --list` shows what exists.")
        return None, route
    sure = route["use_case_probability"] * route["skill_probability"]
    console.say(f"route: {path}  ({sure:.0%}, {route['latency_ms']} ms)")
    if sure < SURE and not console.ask(f"  Not sure that's right. Run {path}? [y/N] "):
        console.say("  nothing ran.")
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
        console.say(f"  no report: {report['error']}")
        return
    for entry in report["entries"]:
        console.say(f"  {entry['name']:<28} {entry['when'] or '':<10} {entry['text'] or ''}")
    left_out = len(report["dropped"])
    if not report["entries"]:
        why = report["missing"] or "the page showed none of what you asked for."
        if left_out:
            why = f"{left_out} entries could not be copied exactly from the page."
        console.say(f"  Nothing to report: {why}")
    elif left_out:
        console.say(f"  ({left_out} entries had values left out: not found word for word on the page)")
    console.say(
        f"  (what the page showed when the run finished{'; the trace has the full text' if mode == 'full' else ''})"
    )


def run_browser(skill, details, close=None, route=None, background=None, trace=None):
    goal = f"{skill.task}\n{details}" if details else skill.task
    mode = trace or config.get("trace")
    background = skill.background if background is None else background
    # Under `jev serve` a background run keeps its tab warm for the next request to that site, unless --close or
    # --no-close says otherwise. A one-off run has no next request, and an unseen tab is clutter, so it closes it.
    tabs = browser.TABS if background and close is None else None
    close = background if close is None else close
    console.say(f"{skill.path} → {skill.url}{'  (background tab)' if background else ''}")
    warm = tabs.open(skill.url) if tabs else None
    agent = Agent(
        skill.url, goal, rules=skill.rules, confirm=skill.confirm, approve=approve, background=background, browser=warm
    )
    shown, report = 0, None
    try:
        for state in agent.run():
            for step in state["history"][shown:]:
                console.say(describe(step))
            shown = len(state["history"])
    except console.Stopped as stop:
        agent.state["status"] = stop.reason  # stopped between actions; the trace still gets written
    finally:
        state = agent.snapshot()
        if skill.report and state["status"] == "done":
            report = make_report(goal, skill, state)
        path = save_trace(skill, {"route": route, **state, "tab": agent.browser.tab, "report": report}, mode)
        if state["status"] == "uncertain":
            pass  # leave the tab exactly as it is, out of the pool, so you can see whether the input landed
        elif tabs:
            tabs.keep(agent.browser, skill.url, state["status"])
        elif close:
            agent.close()
    outcome = {
        "done": "DONE: the model reports success."
        + (" Check the tab to confirm." if not close else " The trace has the page it saw." if mode == "full" else ""),
        "blocked": "BLOCKED: no supported action could make progress.",
        "declined": "STOPPED: a confirm action was not approved.",
        "timeout": f"TIMEOUT: stopped between actions after {config.get('run_timeout_seconds')} s.",
        "cancelled": "CANCELLED: stopped between actions.",
        "uncertain": "UNCERTAIN: Chrome never confirmed the last action, so it may or may not have happened. "
        + ("The tab is left open in the background; look before running again." if background else "Check the tab."),
    }[state["status"]]
    console.say(f"{state['elapsed_ms']:>6} ms  {outcome}")
    if path:
        console.say(f"{'':>9}  trace: {path}")
    if report:
        print_report(report, mode)
    return exit_code(state["status"])


def exit_code(status):
    return 0 if status == "done" else 130 if status == "cancelled" else 1


def run_api(skill, details, route=None, trace=None):
    service, _, name = skill.api.partition(".")
    operation = APIS.get(service, {}).get(name)
    if operation is None:
        raise ValueError(f"{skill.path}: unknown api {skill.api!r}")
    console.say(f"{skill.path} → {skill.api}")
    try:
        record = operation(skill, details, confirm)
    except console.Stopped as stop:
        # Stopped only before a model call or a confirmation, so nothing was changed.
        record = {"status": stop.reason}
    path = save_trace(skill, {"route": route, **record}, trace or config.get("trace"))
    outcome = {
        "done": "done",
        "declined": "STOPPED: not confirmed; nothing changed.",
        "timeout": f"TIMEOUT: stopped after {config.get('run_timeout_seconds')} s; nothing changed.",
        "cancelled": "CANCELLED: nothing changed.",
    }[record["status"]]
    console.say(f"  {outcome}" + (f"\n  trace: {path}" if path else ""))
    return exit_code(record["status"])


def execute(args):
    """Run one parsed request through the current console. Returns the exit code; errors are said, not raised."""
    request = " ".join(args.request).strip()
    console.current().budget(config.get("run_timeout_seconds"))
    try:
        if args.list:
            list_skills()
            return 0
        if args.skill:
            skill, route = skills.load(args.skill), None
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
        console.say(f"jev: {error}")
        return 1
    except TimeoutError as error:  # Chrome went quiet while a tab was being opened; nothing was sent to the page
        console.say(f"jev: Chrome did not answer in time ({error})")
        return 1
    except console.Stopped as stop:  # before a skill was chosen, so nothing ran
        console.say(f"jev: {stop.reason} before anything ran")
        return exit_code(stop.reason)


def main(argv=None):
    """Run a request in this process, as `jev --no-server` does."""
    args = parse(argv)
    load_environment()
    try:
        return execute(args)
    except KeyboardInterrupt:
        raise SystemExit("jev: interrupted") from None


if __name__ == "__main__":
    sys.exit(main())
