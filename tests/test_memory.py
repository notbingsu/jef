"""The single pending-question slot, and the follow-up it enables. Offline: routing and operations are scripted."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jev_ultrafast import cli, config, memory

TREE = (
    ("moomoo/_branch.toml", 'description = "Brokerage"\n'),
    ("moomoo/activity.toml", 'description = "Fills"\ntask = "Show activity."\napi = "moomoo.activity"\n'),
    ("moomoo/holdings.toml", 'description = "Holdings"\ntask = "Show holdings."\napi = "moomoo.holdings"\n'),
)
ASKED = "today only, or the last week?"


@pytest.fixture
def here(tmp_path, monkeypatch):
    """Run in a scratch directory, so the slot and any traces land under tmp_path rather than the project."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def tree(here, monkeypatch):
    for path, text in TREE:
        file = here / "tree" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    monkeypatch.setenv("JEV_SKILLS_DIR", str(here / "tree"))
    monkeypatch.setattr(cli, "load_environment", lambda: None)
    return here


@pytest.fixture
def ops(monkeypatch):
    """A fake api service. `replies` is consumed one per call; an exception is raised instead of returned."""
    calls, replies = [], []

    def operation(skill, details, confirm):
        calls.append((skill.path, details))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setitem(cli.APIS, "moomoo", {"activity": operation, "holdings": operation})
    return calls, replies


def routing(path, follow_up=0.9):
    """A router stub shaped like the real one: the follow-up head exists only when a question is pending."""

    def route(request, root=None, pending=None):
        record = {"use_case_probability": 0.9, "skill_probability": 0.9, "latency_ms": 5}
        if pending:
            record["follow_up"] = follow_up
        return path, record

    return route


# --- the slot itself -------------------------------------------------------------------------------------------

def test_a_question_is_kept_and_read_back(here):
    memory.remember("moomoo/activity", "what was my latest stock buy", ASKED)
    slot = memory.pending()
    assert (slot["skill"], slot["question"], slot["carries"]) == ("moomoo/activity", ASKED, 1)
    assert slot["earlier_request"] == "what was my latest stock buy"


def test_the_slot_is_readable_only_by_you(here):
    memory.remember("s", "r", "q")
    assert memory.path().stat().st_mode & 0o077 == 0


def test_a_question_older_than_memory_minutes_is_dropped(here):
    memory.remember("s", "r", "q")
    stale = datetime.now(timezone.utc) - timedelta(minutes=config.get("memory_minutes") + 1)
    memory.path().write_text(json.dumps({**json.loads(memory.path().read_text()), "at": stale.isoformat()}))
    assert memory.pending() is None and not memory.path().exists()


def test_a_slot_that_cannot_be_read_is_removed_rather_than_consulted_twice(here):
    memory.path().parent.mkdir(parents=True, exist_ok=True)
    memory.path().write_text("not json")
    assert memory.pending() is None and not memory.path().exists()


def test_a_request_too_long_to_grow_is_not_kept(here):
    assert memory.remember("s", "x" * (memory.LENGTH + 1), "q") is None and not memory.path().exists()


def test_merge_keeps_both_halves_in_the_order_they_were_said(here):
    slot = memory.remember("s", "what was my latest stock buy", "q")
    assert memory.merge(slot, "within last month") == "what was my latest stock buy\nwithin last month"


def test_unanswered_is_a_value_error_so_existing_callers_are_unchanged():
    error = memory.Unanswered(ASKED, f"Need more detail: {ASKED}")
    assert isinstance(error, ValueError) and error.question == ASKED
    assert str(error) == f"Need more detail: {ASKED}"


# --- one question, one answer ----------------------------------------------------------------------------------

def test_an_unanswered_run_traces_its_question_and_keeps_it(tree, ops, monkeypatch):
    calls, replies = ops
    replies.append(memory.Unanswered(ASKED, f"Need more detail: {ASKED}"))
    monkeypatch.setattr(cli.router, "route", routing("moomoo/activity"))
    assert cli.main(["what", "was", "my", "latest", "stock", "buy"]) == 1
    slot = memory.pending()
    assert (slot["skill"], slot["earlier_request"], slot["carries"]) == ("moomoo/activity", calls[0][1], 1)
    # The case you most want a record of used to be the one case that wrote none.
    trace = json.loads(next((tree / "artifacts" / "runs").rglob("*.json")).read_text())
    assert trace["status"] == "unanswered" and trace["question"] == ASKED
    assert trace["goal"].endswith("what was my latest stock buy")


def test_a_follow_up_runs_the_skill_that_asked_with_both_halves(tree, ops, monkeypatch, capsys):
    calls, replies = ops
    memory.remember("moomoo/activity", "what was my latest stock buy", ASKED)
    replies.append({"status": "done"})
    # The reply on its own would route elsewhere; the follow-up head decides, not the fragment.
    monkeypatch.setattr(cli.router, "route", routing("moomoo/holdings", follow_up=0.9))
    assert cli.main(["within", "last", "month"]) == 0
    assert calls == [("moomoo/activity", "what was my latest stock buy\nwithin last month")]
    assert memory.pending() is None  # answered, and this run did not ask another
    assert "continuing moomoo/activity" in capsys.readouterr().out


def test_a_request_that_is_not_an_answer_routes_normally(tree, ops, monkeypatch):
    calls, replies = ops
    memory.remember("moomoo/activity", "what was my latest buy", ASKED)
    replies.append({"status": "done"})
    monkeypatch.setattr(cli.router, "route", routing("moomoo/holdings", follow_up=0.2))
    assert cli.main(["what", "do", "I", "hold"]) == 0
    assert calls == [("moomoo/holdings", "what do I hold")]
    # TODO: an unrelated request does not invalidate the slot yet; only time does.
    assert memory.pending() is not None


def test_an_explicit_skill_ignores_a_pending_question(tree, ops):
    calls, replies = ops
    memory.remember("moomoo/activity", "earlier words", ASKED)
    replies.append({"status": "done"})
    assert cli.main(["--skill", "moomoo/holdings", "what", "do", "I", "hold"]) == 0
    assert calls == [("moomoo/holdings", "what do I hold")]


# --- a window the run chose itself -----------------------------------------------------------------------------

def test_an_assumed_window_is_offered_and_can_be_replaced(tree, ops, monkeypatch):
    """A run that got through on a window it chose leaves that choice offered, so the next request replaces it
    instead of starting over."""
    calls, replies = ops
    monkeypatch.setattr(cli.router, "route", routing("moomoo/activity"))
    replies += [
        {"status": "done", "assumed": "No range was given, so I read the last month. Say another range."},
        {"status": "done"},
    ]
    assert cli.main(["what", "was", "my", "latest", "trade"]) == 0
    slot = memory.pending()
    assert slot["skill"] == "moomoo/activity" and "last month" in slot["question"]

    assert cli.main(["just", "this", "week"]) == 0
    assert calls[-1] == ("moomoo/activity", "what was my latest trade\njust this week")
    # The replacement named a range, so nothing is assumed any more and nothing is left offered.
    assert memory.pending() is None


# --- a follow-up to a follow-up --------------------------------------------------------------------------------

def test_an_answer_to_an_answer_compounds(tree, ops, monkeypatch):
    """The slot holds the merged request, so a second answer lands on top of the first rather than replacing it."""
    calls, replies = ops
    monkeypatch.setattr(cli.router, "route", routing("moomoo/activity"))
    replies += [memory.Unanswered(ASKED), memory.Unanswered("which account?"), {"status": "done"}]

    assert cli.main(["what", "was", "my", "latest", "buy"]) == 1
    assert memory.pending()["carries"] == 1

    assert cli.main(["within", "last", "month"]) == 1
    slot = memory.pending()
    assert slot["carries"] == 2 and slot["question"] == "which account?"
    assert slot["earlier_request"] == "what was my latest buy\nwithin last month"

    assert cli.main(["the", "margin", "one"]) == 0
    assert memory.pending() is None
    assert [details for _, details in calls] == [
        "what was my latest buy",
        "what was my latest buy\nwithin last month",
        "what was my latest buy\nwithin last month\nthe margin one",
    ]


def test_a_chain_is_dropped_once_it_is_deeper_than_CARRIES(tree, ops, monkeypatch):
    calls, replies = ops
    monkeypatch.setattr(cli.router, "route", routing("moomoo/activity"))
    replies += [memory.Unanswered("again?")] * (memory.CARRIES + 1)
    for _ in range(memory.CARRIES + 1):
        assert cli.main(["more"]) == 1
    # The last remember was one carry too deep, so the chain is gone and the next request starts fresh.
    assert memory.pending() is None
    replies.append({"status": "done"})
    assert cli.main(["something", "else"]) == 0
    assert calls[-1] == ("moomoo/activity", "something else")
