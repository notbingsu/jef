"""Headless runs and the report printed to the CLI. Offline: scripted text-model answers, a fake agent, no Chrome."""

from types import SimpleNamespace

import pytest

from jev_ultrafast import browser, cli, model
from jev_ultrafast.skills import Skill

PAGE_TEXT = """Messaging
Search messages
Angela Xu, PhD
Oct 1
Oct 1
Sponsored
Bingyuan, your invitation — First Wave AI Fellowship
Katharine Tan
Sep 29
Sep 29
Katharine: Thanks for sending me your resume.
Speak soon!
Joy Z
Sep 22
You: Hi Joy, my pleasure to connect!"""
PAGE = {"url": "https://www.linkedin.com/messaging/", "title": "(14) LinkedIn", "text": PAGE_TEXT}


def entry(name, when=None, text=None):
    return {"name": name, "when": when, "text": text}


def reply(monkeypatch, output):
    sent = []

    def complete_json(system, context, schema, purpose):
        sent.append(context)
        return output, {"model": "test", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(model, "complete_json", complete_json)
    return sent


def test_values_are_kept_only_when_copied_word_for_word_from_the_page(monkeypatch):
    sent = reply(
        monkeypatch,
        {
            "entries": [
                entry("Katharine Tan", "Sep 29", "Katharine: Thanks for sending me your resume. Speak soon!"),
                entry("Joy Z", "Sep 22", "You: Nice to connect!"),  # paraphrased preview
                entry("Rohan Bahl", "Sep 22", "You: as expected"),  # never on the page
                entry(""),  # no name
            ],
            "missing": None,
        },
    )
    entries, missing, dropped, _ = model.page_report("Read my DMs", PAGE, ("Skip ads.",))
    # Line breaks in the page are whitespace, so a preview wrapped over two lines still matches.
    assert entries[0] == {
        "name": "Katharine Tan",
        "when": "Sep 29",
        "text": "Katharine: Thanks for sending me your resume. Speak soon!",
    }
    # A value that is not on the page is blanked and the verified rest stays; a bad name drops the entry.
    assert entries[1:] == [{"name": "Joy Z", "when": "Sep 22", "text": None}]
    assert [(d["entry"]["name"], d["fields"]) for d in dropped] == [
        ("Joy Z", ["text"]),
        ("Rohan Bahl", ["name", "text"]),
        ("", ["name"]),
    ]
    assert missing is None
    assert sent[0]["page"]["text"] == PAGE_TEXT and sent[0]["skill_rules"] == ["Skip ads."]


@pytest.mark.parametrize(
    "output",
    [
        None,
        {"entries": "none", "missing": None},
        {"entries": [], "missing": None, "extra": 1},
        {"entries": [{"name": 1}]},
    ],
)
def test_a_report_that_does_not_match_the_schema_shows_nothing(monkeypatch, output):
    reply(monkeypatch, output)
    with pytest.raises(ValueError, match="did not match"):
        model.page_report("Read my DMs", PAGE)


class FakeAgent:
    made = []

    def __init__(self, url, goal, **options):
        self.options, self.closed = options, False
        self.browser = options.get("browser") or SimpleNamespace(tab="new")
        self.state = {"status": "done", "page": PAGE, "history": [], "elapsed_ms": 5}
        FakeAgent.made.append(self)

    def run(self):
        yield self.state

    def snapshot(self):
        return self.state

    def close(self):
        self.closed = True


@pytest.fixture
def agent(monkeypatch, tmp_path):
    FakeAgent.made = []
    monkeypatch.setattr(cli, "Agent", FakeAgent)
    monkeypatch.chdir(tmp_path)  # traces land in tmp_path/artifacts
    return FakeAgent


def skill(**flags):
    return Skill(path="linkedin-dms/check", task="Read DMs.", url="https://www.linkedin.com/messaging/", **flags)


def test_a_background_skill_reports_to_the_cli_and_closes_its_tab(agent, monkeypatch, capsys):
    reply(monkeypatch, {"entries": [entry("Joy Z", "Sep 22", "You: Hi Joy, my pleasure to connect!")], "missing": None})
    assert cli.run_browser(skill(background=True, report=True), "check my dms") == 0
    out = capsys.readouterr().out
    assert "(background tab)" in out and out.index("DONE") < out.index("Joy Z")
    assert "Joy Z" in out and "Sep 22" in out and "my pleasure to connect" in out
    assert agent.made[0].options["background"] is True and agent.made[0].closed
    trace = next((cli.Path("artifacts") / "runs").rglob("*.json")).read_text()
    assert '"entries"' in trace and "my pleasure to connect" in trace


def test_no_background_flag_overrides_the_skill_and_leaves_the_tab_open(agent, monkeypatch, capsys):
    reply(monkeypatch, {"entries": [], "missing": "The list is empty."})
    cli.run_browser(skill(background=True, report=True), "check my dms", background=False)
    out = capsys.readouterr().out
    assert "(background tab)" not in out and "Nothing to report: The list is empty." in out
    assert agent.made[0].options["background"] is False and not agent.made[0].closed


def test_close_can_be_asked_for_either_way(agent, monkeypatch):
    monkeypatch.setattr(model, "complete_json", lambda *a: pytest.fail("no report was asked for"))
    cli.run_browser(skill(), "check my dms", close=True)
    assert agent.made[0].closed
    cli.run_browser(skill(background=True), "check my dms", close=False)
    assert not agent.made[1].closed


def test_a_skill_without_report_prints_no_entries_and_asks_no_model(agent, monkeypatch, capsys):
    monkeypatch.setattr(model, "complete_json", lambda *a: pytest.fail("no report was asked for"))
    cli.run_browser(skill(), "check my dms")
    assert "what the page showed" not in capsys.readouterr().out


def test_a_failed_report_does_not_hide_the_run(agent, monkeypatch, capsys):
    reply(monkeypatch, None)
    assert cli.run_browser(skill(report=True), "check my dms") == 0
    assert "no report:" in capsys.readouterr().out


def fake_cdp(monkeypatch, fail_at=None):
    """Replace the daemon and CDP with fakes; returns the log of calls."""
    log = []

    def cdp(method, **params):
        log.append((method, params))
        if method == fail_at:
            raise RuntimeError("boom")
        return {"targetId": "t", "sessionId": "s"}

    monkeypatch.setattr(browser, "ensure_daemon", lambda: log.append(("daemon", {})))
    monkeypatch.setattr(browser, "cdp", cdp)
    monkeypatch.setattr(browser.Browser, "evaluate", lambda self, expression: "complete")
    return log


def opened_in_background(log):
    return next(params["background"] for method, params in log if method == "Target.createTarget")


def test_a_background_run_opens_its_tab_in_the_background_of_the_same_chrome(monkeypatch):
    log = fake_cdp(monkeypatch)
    opened = browser.Browser("https://example.test/", background=True)
    assert log[0] == ("daemon", {}) and opened_in_background(log) is True
    # Nothing re-activates the tab, so your Chrome stays where it is.
    assert not [method for method, _ in log if method == "Target.activateTarget"]
    opened.close()
    opened.close()  # closing twice is harmless
    assert [method for method, _ in log].count("Target.closeTarget") == 1


def test_a_visible_run_still_opens_in_the_foreground(monkeypatch):
    log = fake_cdp(monkeypatch)
    browser.Browser("https://example.test/")
    assert opened_in_background(log) is False


def test_a_tab_is_never_left_behind_when_setup_fails(monkeypatch):
    log = fake_cdp(monkeypatch, fail_at="Target.attachToTarget")
    with pytest.raises(RuntimeError, match="boom"):
        browser.Browser("https://example.test/", background=True)
    assert ("Target.closeTarget", {"targetId": "t"}) in log
