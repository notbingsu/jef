"""Slow, silent and stopped runs. Offline: fake browsers, fake HTTP, no paid calls."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_agent import decision, page

from jev_ultrafast import agent as loop
from jev_ultrafast import browser, cli, console, gcal, model
from jev_ultrafast.skills import Skill


@pytest.fixture
def runner():
    """An agent holding a decision to click e3, with a browser that has not been asked to do anything yet."""
    a = loop.Agent.__new__(loop.Agent)
    a.rules, a.confirm, a.approve, a.pending_text = (), (), None, None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": {**decision("e3"), "operation": "CLICK", "probabilities": {"e3": 1.0}},
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "text_calls": [],
    }
    return a


@pytest.fixture
def out_of_time():
    clock = console.current()
    clock.budget(0.001)
    time.sleep(0.01)
    yield clock
    clock.deadline = None


def act(runner):
    return runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})


def test_an_input_chrome_never_confirms_is_logged_once_and_ends_the_run(runner):
    runner.state["browser"].act.side_effect = TimeoutError("Input.dispatchMouseEvent timed out after 5s")
    act(runner)
    assert runner.state["browser"].act.call_count == 1  # never retried
    assert runner.state["status"] == "uncertain"
    assert runner.state["history"][-1]["unconfirmed"] and runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].observe.assert_not_called()
    with pytest.raises(ValueError, match="stopped"):
        runner.command("predict")  # an uncertain run is over


def test_the_clock_stops_a_run_before_an_action_and_never_after_one(runner, out_of_time):
    with pytest.raises(console.Stopped, match="timeout"):
        act(runner)
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None and not runner.state["history"]


def test_the_clock_stops_a_run_before_a_decision(runner, out_of_time, monkeypatch):
    monkeypatch.setattr(loop, "choose", lambda *a: pytest.fail("no decision after the deadline"))
    runner.state["status"] = "ready"
    with pytest.raises(console.Stopped):
        runner.command("predict")


def bare_browser():
    opened = browser.Browser.__new__(browser.Browser)
    opened.session, opened.target, opened.broken, opened.after_input = "s", "t", False, None
    return opened


def test_a_silent_read_is_a_stale_page(monkeypatch):
    monkeypatch.setattr(browser, "cdp", Mock(side_effect=TimeoutError("Runtime.evaluate timed out")))
    with pytest.raises(browser.StalePage, match="did not answer"):
        bare_browser().evaluate("1")


def test_three_silent_observations_mean_chrome_is_stuck(monkeypatch):
    reads = Mock(side_effect=TimeoutError("observe timed out"))
    monkeypatch.setattr(browser, "browser_operation", reads)
    stuck = bare_browser()
    with pytest.raises(RuntimeError, match="stopped responding"):
        stuck.observe()
    assert reads.call_count == 3 and stuck.broken
    # The pool drops a broken tab rather than keeping it warm.
    monkeypatch.setattr(browser, "cdp", Mock(return_value={}))
    tabs = browser.Tabs()
    tabs.keep(stuck, "https://www.linkedin.com/messaging/", "done")
    assert not tabs.warm


def test_one_slow_observation_is_simply_retried(monkeypatch):
    p = page()
    monkeypatch.setattr(browser, "browser_operation", Mock(side_effect=[TimeoutError("slow"), p]))
    assert bare_browser().observe() == p


class FakeAgent:
    def __init__(self, url, goal, stop=None, uncertain=False, **options):
        self.closed = False
        self.browser = SimpleNamespace(tab="new", broken=False, target="t")
        self.state = {"status": "uncertain" if uncertain else "ready", "page": page(), "history": [], "elapsed_ms": 1}
        self.stop = stop

    def run(self):
        if self.stop:
            raise console.Stopped(self.stop)
        yield self.state

    def snapshot(self):
        return self.state

    def close(self):
        self.closed = True


SKILL = Skill(path="linkedin-dms/check", task="Read.", url="https://www.linkedin.com/messaging/", background=True)


@pytest.mark.parametrize("reason, code", [("timeout", 1), ("cancelled", 130)])
def test_a_stopped_browser_run_still_writes_its_trace(monkeypatch, tmp_path, capsys, reason, code):
    monkeypatch.chdir(tmp_path)
    made = []
    monkeypatch.setattr(cli, "Agent", lambda *a, **k: made.append(FakeAgent(*a, stop=reason, **k)) or made[-1])
    assert cli.run_browser(SKILL, "check") == code
    assert reason.upper() in capsys.readouterr().out
    assert next((tmp_path / "artifacts").rglob("*.json")).read_text().count(f'"status": "{reason}"') == 1
    assert made[0].closed  # a one-off background run cleans up, stopped or not


def test_an_uncertain_tab_is_left_open_and_out_of_the_pool(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    made = []
    monkeypatch.setattr(cli, "Agent", lambda *a, **k: made.append(FakeAgent(*a, uncertain=True, **k)) or made[-1])
    pool = Mock()
    pool.open.return_value = None
    monkeypatch.setattr(browser, "TABS", pool)
    assert cli.run_browser(SKILL, "check") == 1
    assert "left open in the background" in capsys.readouterr().out
    assert not made[0].closed and not pool.keep.called


def test_a_stopped_api_run_changed_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    skill = Skill(path="calendar/find-events", task="Find.", url="", api="gcal.find_events")
    monkeypatch.setitem(cli.APIS["gcal"], "find_events", Mock(side_effect=console.Stopped("timeout")))
    assert cli.run_api(skill, "any events") == 1
    assert "nothing changed" in capsys.readouterr().out


def test_a_dropped_model_connection_is_retried_once_and_a_timeout_never(monkeypatch):
    ok = httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://m.test"))
    calls = Mock(side_effect=[httpx.ConnectError("reset"), ok])
    monkeypatch.setattr(model, "CLIENT", SimpleNamespace(post=calls))
    assert model.post_json("https://m.test", "k", {}) == {"ok": True} and calls.call_count == 2
    calls = Mock(side_effect=[httpx.ReadError("reset"), httpx.ReadError("reset again")])
    monkeypatch.setattr(model, "CLIENT", SimpleNamespace(post=calls))
    with pytest.raises(RuntimeError, match="connection failed"):
        model.post_json("https://m.test", "k", {})
    calls = Mock(side_effect=httpx.ReadTimeout("slow"))
    monkeypatch.setattr(model, "CLIENT", SimpleNamespace(post=calls))
    with pytest.raises(RuntimeError):
        model.post_json("https://m.test", "k", {})
    assert calls.call_count == 1


def calendar():
    found = gcal.Calendar.__new__(gcal.Calendar)
    found.path, found.creds = "/calendars/primary", SimpleNamespace(valid=True, token="t")
    return found


def test_a_calendar_lookup_is_retried_once_and_a_change_never(monkeypatch):
    ok = httpx.Response(200, json={"items": []}, request=httpx.Request("GET", "https://g.test"))
    lookups = Mock(side_effect=[httpx.ReadError("reset"), ok])
    monkeypatch.setattr(gcal, "CLIENT", SimpleNamespace(request=lookups))
    assert calendar().call("GET", "/events") == {"items": []} and lookups.call_count == 2
    changes = Mock(side_effect=httpx.ReadError("reset"))
    monkeypatch.setattr(gcal, "CLIENT", SimpleNamespace(request=changes))
    with pytest.raises(RuntimeError, match="check the calendar"):
        calendar().call("POST", "/events", json={})
    assert changes.call_count == 1


def test_the_calendar_stops_before_a_model_call_once_out_of_time(out_of_time, monkeypatch):
    monkeypatch.setattr(gcal, "complete_json", lambda *a: pytest.fail("no model call after the deadline"))
    skill = Skill(path="calendar/find-events", task="Find.", url="", api="gcal.find_events")
    with pytest.raises(console.Stopped):
        gcal.ask(skill, "any events", gcal.ZoneInfo("UTC"), gcal.SEARCH, "Find.")
