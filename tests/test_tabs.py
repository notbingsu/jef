"""Warm background tabs under jev serve. Offline: a fake Chrome stands in for CDP."""

import pytest

from jev_ultrafast import browser

SKILL = "https://www.linkedin.com/messaging/"


class Chrome:
    """Enough CDP for tab lifecycle: targets, sessions, a page URL per tab, and tabs that stop answering."""

    def __init__(self):
        self.targets, self.href, self.frozen, self.calls = set(), {}, set(), []
        self.sessions, self.made = {}, 0

    def __call__(self, method, session_id=None, _response_timeout=None, **params):
        self.calls.append((method, session_id, params))
        target = self.sessions.get(session_id)
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page"} for t in self.targets]}
        if method == "Target.createTarget":
            self.made += 1
            target = f"t{self.made}"
            self.targets.add(target)
            self.href[target] = params["url"]
            return {"targetId": target}
        if method == "Target.attachToTarget":
            session = f"s-{params['targetId']}-{len(self.calls)}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Target.closeTarget":
            self.targets.discard(params["targetId"])
            return {}
        if method == "Page.navigate":
            self.href[target] = params["url"]
            return {}
        if method == "Runtime.evaluate":
            if target in self.frozen:
                raise TimeoutError("Runtime.evaluate timed out after 2s waiting for the daemon")
            values = {"1": 1, "document.readyState": "complete", "location.href": self.href.get(target)}
            return {"result": {"value": values.get(params["expression"])}}
        return {}

    def methods(self, since=0):
        return [method for method, _, _ in self.calls[since:]]


@pytest.fixture
def chrome(monkeypatch):
    fake = Chrome()
    monkeypatch.setattr(browser, "cdp", fake)
    monkeypatch.setattr(browser, "ensure_daemon", lambda **kwargs: None)
    monkeypatch.setattr(browser, "daemon_browser_ready", lambda: True)
    return fake


def run(tabs, url, status="done"):
    """Open a tab from the pool as a run would, then hand it back with the run's status."""
    opened = tabs.open(url)
    tabs.keep(opened, url, status)
    return opened


def test_a_done_run_leaves_a_tab_the_next_run_reuses_in_place(chrome):
    tabs = browser.Tabs()
    first = run(tabs, SKILL)
    assert first.tab == "new"
    chrome.href[first.target] = SKILL + "thread/2-abc/"  # LinkedIn opens the newest thread by itself
    mark = len(chrome.calls)
    second = run(tabs, SKILL)
    assert second.tab == "reused" and second.target == first.target
    assert "Page.navigate" not in chrome.methods(mark)
    # The overrides belong to the session, so the re-attached tab gets them again.
    assert {"Emulation.setDeviceMetricsOverride", "Emulation.setFocusEmulationEnabled"} <= set(chrome.methods(mark))


@pytest.mark.parametrize("status", ["blocked", "timeout", "cancelled", "declined"])
def test_a_run_that_did_not_finish_sends_the_next_one_back_to_the_skill_url(chrome, status):
    tabs = browser.Tabs()
    first = run(tabs, SKILL, status)
    mark = len(chrome.calls)
    second = run(tabs, SKILL)
    assert second.tab == "navigated" and second.target == first.target
    assert "Page.navigate" in chrome.methods(mark)


def test_a_tab_moved_off_the_skill_url_is_navigated_back(chrome):
    tabs = browser.Tabs()
    first = run(tabs, SKILL)
    chrome.href[first.target] = "https://www.linkedin.com/feed/"
    assert run(tabs, SKILL).tab == "navigated"


def test_a_closed_tab_is_replaced(chrome):
    tabs = browser.Tabs()
    first = run(tabs, SKILL)
    chrome.targets.discard(first.target)  # you closed it, or Chrome restarted
    second = run(tabs, SKILL)
    assert second.tab == "new" and second.target != first.target


def test_a_tab_that_stops_answering_is_closed_and_replaced(chrome):
    tabs = browser.Tabs()
    first = run(tabs, SKILL)
    chrome.frozen.add(first.target)
    second = run(tabs, SKILL)
    assert second.tab == "new" and first.target not in chrome.targets


def test_tabs_are_kept_per_site_released_between_runs_and_closed_with_the_server(chrome):
    tabs = browser.Tabs()
    mark = len(chrome.calls)
    linkedin = run(tabs, SKILL)
    mail = run(tabs, "https://mail.example.com/inbox")
    assert linkedin.target != mail.target and set(tabs.warm) == {"https://www.linkedin.com", "https://mail.example.com"}
    assert chrome.methods(mark).count("Target.detachFromTarget") == 2
    tabs.close_all()
    assert not chrome.targets and not tabs.warm


def test_a_new_connection_says_how_to_approve_and_bounds_the_wait(chrome, monkeypatch, capsys):
    waits = []
    monkeypatch.setattr(browser, "daemon_browser_ready", lambda: False)
    monkeypatch.setattr(browser, "ensure_daemon", lambda **kwargs: waits.append(kwargs.get("wait")))
    browser.Browser(SKILL, background=True)
    assert "click Allow" in capsys.readouterr().out and waits == [120]
