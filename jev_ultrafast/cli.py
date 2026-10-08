"""Running one request: route it, run the skill, report and trace it. `jev serve` calls execute() for each request it
receives; `jev --no-server` calls it in-process. Everything said or asked goes through the current console."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import browser, config, console, gcal, memory, moomoo, router, skills
from .agent import Agent
from .command import load_environment, parse
from .model import page_report

# api = "service.operation" in a leaf skill resolves here.
APIS = {"gcal": gcal.OPERATIONS, "moomoo": moomoo.OPERATIONS}
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


# The difference between the two levels. A low trace says what was passed and what was done; a full trace also says
# how sure each model was, how long it took and what it cost. These keys carry that second part, and a low trace
# drops them wherever they appear, so a new skill's record needs no change here.
METADATA = frozenset(
    {
        # How sure: every probability, and the routing and matching figures derived from them.
        "probability",
        "probabilities",
        "confidence",
        "operation_probabilities",
        "target_probabilities",
        "target_confidence",
        "use_case_probability",
        "skill_probability",
        "answer_needed",
        "match",
        "follow_up",
        # How long, and what it cost.
        "latency_ms",
        "elapsed_ms",
        "executed_ms",
        "text_latency_ms",
        "usage",
        "model",
        "text_helper",
        # The plumbing behind a choice: the bodies sent, the answers as they came back, the per-call records.
        "request",
        "raw",
        "raw_answers",
        "answers",
        "decisions",
        "judgments",
        "model_call",
        "model_calls",
        "text_calls",
        "fingerprint",
    }
)
# Page content: most of a full trace by size, and all of what the page showed.
CONTENT = frozenset({"decision", "elements", "started_at"})


def without_metadata(value):
    """`value` with every METADATA key removed, at any depth."""
    if isinstance(value, dict):
        return {key: without_metadata(item) for key, item in value.items() if key not in METADATA}
    if isinstance(value, list):
        return [without_metadata(item) for item in value]
    return value


def low_trace(trace):
    """What was passed and what was done: the goal in the user's own words, each action and the text it carried, and
    the run's answer. No page text or element table, and none of the metadata behind a choice."""
    kept = without_metadata({k: v for k, v in trace.items() if k not in CONTENT})
    kept["skill"] = {k: trace["skill"][k] for k in ("path", "api", "url")}
    if trace.get("page"):
        kept["page"] = {k: trace["page"].get(k) for k in ("url", "title")}
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
    """Route the request; returns (Skill, routing record, the details to run with), or (None, record, request).

    A question a run stopped on rides in the same routing request as one more head. When this request answers it, the
    skill that asked runs again with both halves, because the answer belongs to the question rather than to whatever
    the reply alone would route to: `within last month` on its own routes at 46%, and `make it september instead`
    routes confidently to a calendar change."""
    slot = memory.pending()
    path, route = router.route(request, pending=slot)
    if slot and route.get("follow_up", 0) >= SURE:
        details = memory.merge(slot, request)
        console.say(
            f"continuing {slot['skill']}, which asked: {slot['question']}",
            item={"kind": "continuing", "skill": slot["skill"], "question": slot["question"]},
        )
        console.say(f"  request: {details.replace(chr(10), '  ·  ')}", item={"kind": "columns"})
        return skills.load(slot["skill"]), {**route, "continued": slot}, details
    if path is None:
        console.say(
            f"route: no skill fits ({route['use_case_probability']:.0%} sure). `jev --list` shows what exists.",
            item={"kind": "note", "text": "No skill fits this. /help lists the skills."},
        )
        return None, route, request
    sure = route["use_case_probability"] * route["skill_probability"]
    # Which leaf, and how sure, is how a run was decided; a chat is shown what the run then found.
    console.say(f"route: {path}  ({sure:.0%}, {route['latency_ms']} ms)", item={"kind": "route", "skill": path})
    if sure < SURE and not console.ask(f"  Not sure that's right. Run {path}? [y/N] "):
        console.say("  nothing ran.")
        return None, route, request
    return skills.load(path), route, request


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
        console.say(f"  no report: {report['error']}", item={"kind": "note", "text": f"no report: {report['error']}"})
        return
    for entry in report["entries"]:
        console.say(
            f"  {entry['name']:<28} {entry['when'] or '':<10} {entry['text'] or ''}",
            item={"kind": "entry", **entry},
        )
    left_out = len(report["dropped"])
    if not report["entries"]:
        why = report["missing"] or "the page showed none of what you asked for."
        if left_out:
            why = f"{left_out} entries could not be copied exactly from the page."
        console.say(f"  Nothing to report: {why}", item={"kind": "note", "text": f"Nothing to report: {why}"})
    elif left_out:
        note = f"({left_out} entries had values left out: not found word for word on the page)"
        console.say(f"  {note}", item={"kind": "note", "text": note})
    ending = f"(what the page showed when the run finished{'; the trace has the full text' if mode == 'full' else ''})"
    console.say(f"  {ending}", item={"kind": "columns"})


def run_browser(skill, details, close=None, route=None, background=None, trace=None):
    goal = f"{skill.task}\n{details}" if details else skill.task
    mode = trace or config.get("trace")
    background = skill.background if background is None else background
    # Under `jev serve` a background run keeps its tab warm for the next request to that site, unless --close or
    # --no-close says otherwise. A one-off run has no next request, and an unseen tab is clutter, so it closes it.
    tabs = browser.TABS if background and close is None else None
    close = background if close is None else close
    console.say(
        f"{skill.path} → {skill.url}{'  (background tab)' if background else ''}",
        # Which skill is running is what tells a front end how to lay the rest of the run out.
        item={"kind": "skill", "path": skill.path, "url": skill.url},
    )
    warm = tabs.open(skill.url) if tabs else None
    agent = Agent(
        skill.url, goal, rules=skill.rules, confirm=skill.confirm, approve=approve, background=background, browser=warm
    )
    shown, report = 0, None
    try:
        for state in agent.run():
            for step in state["history"][shown:]:
                console.say(describe(step), item={"kind": "step", "operation": step["operation"]})
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
    console.say(
        f"{state['elapsed_ms']:>6} ms  {outcome}",
        item={"kind": "outcome", "status": state["status"], "text": outcome},
    )
    if path:
        console.say(f"{'':>9}  trace: {path}", item={"kind": "trace", "path": str(path)})
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
    console.say(f"{skill.path} → {skill.api}", item={"kind": "skill", "path": skill.path, "api": skill.api})
    goal = f"{skill.task}\n{details}" if details else skill.task
    mode = trace or config.get("trace")
    carried = (route or {}).get("continued") or {}
    try:
        record = operation(skill, details, confirm)
    except console.Stopped as stop:
        # Stopped only before a model call or a confirmation, so nothing was changed.
        record = {"status": stop.reason}
    except memory.Unanswered as unanswered:
        # It stopped needing something only you can say. Keep the question for one follow-up, and trace the run:
        # the case you most want a record of used to be the one case that wrote none.
        memory.remember(skill.path, details, unanswered.question, carried.get("carries", 0) + 1)
        asked = save_trace(
            skill, {"route": route, "goal": goal, "status": "unanswered", "question": unanswered.question}, mode
        )
        if asked:
            console.say(f"  trace: {asked}", item={"kind": "trace", "path": str(asked)})
        raise
    path = save_trace(skill, {"route": route, "goal": goal, **record}, mode)
    if assumed := record.get("assumed"):
        # The run got through, but on something it chose for you. Offer that choice back, so the next request can
        # replace it instead of starting over.
        memory.remember(skill.path, details, assumed, carried.get("carries", 0) + 1)
    elif carried:
        memory.forget()  # the question has been answered, and this run did not ask another
    outcome = {
        "done": "done",
        "declined": "STOPPED: not confirmed; nothing changed.",
        "timeout": f"TIMEOUT: stopped after {config.get('run_timeout_seconds')} s; nothing changed.",
        "cancelled": "CANCELLED: nothing changed.",
    }[record["status"]]
    console.say(f"  {outcome}", item={"kind": "outcome", "status": record["status"], "text": outcome})
    if path:
        console.say(f"  trace: {path}", item={"kind": "trace", "path": str(path)})
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
            # Explicit is explicit: a named skill runs the words as given, with no pending question merged in.
            skill, route, details = skills.load(args.skill), None, request
        else:
            skill, route, details = pick(request)
            if skill is None:
                return 1
        if args.route_only:
            return 0
        if skill.api:
            return run_api(skill, details, route, args.trace)
        return run_browser(skill, details, args.close, route, args.background, args.trace)
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
