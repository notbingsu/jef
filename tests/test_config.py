"""jev.toml settings and trace modes. Offline."""

import json

import pytest

from jev_ultrafast import cli, config
from jev_ultrafast.skills import Skill


def settings(tmp_path, text):
    config.path().write_text(text)
    config.read.cache_clear()


def test_defaults_without_a_file(tmp_path):
    config.path().unlink()
    config.read.cache_clear()
    assert config.get("trace") == "full" and config.get("typesafe_model") == "jev-latest"
    assert config.get("server") is True and config.get("server_idle_minutes") == 10


def test_file_values_override_defaults(tmp_path):
    settings(tmp_path, 'trace = "low"\ntext_model = "claude-haiku-4-5"\n')
    assert config.get("trace") == "low" and config.get("text_model") == "claude-haiku-4-5"
    assert config.get("text_model_reasoning") == "low"


@pytest.mark.parametrize(
    "text, message",
    [
        ('trace = "verbose"\n', "trace must be one of full, low, off"),
        ('trcae = "low"\n', "unknown keys"),
        ("trace = 1\n", "must be a non-empty string"),
        ('TYPESAFE_API_KEY = "sk-..."\n', "unknown keys"),
        ("trace = \n", "jev.toml"),
        ('server = "yes"\n', "server must be true or false"),
        ("server_idle_minutes = 0\n", "positive whole number"),
        ("run_timeout_seconds = true\n", "positive whole number"),
        ("approval_wait_seconds = 1.5\n", "positive whole number"),
        ("telegram_allowed = 123\n", "list of positive whole numbers"),
        ('telegram_allowed = ["me"]\n', "list of positive whole numbers"),
        ("telegram_allowed = [0]\n", "list of positive whole numbers"),
        ("telegram_allowed = [true]\n", "list of positive whole numbers"),
    ],
)
def test_bad_settings_fail_loudly(tmp_path, text, message):
    settings(tmp_path, text)
    with pytest.raises(ValueError, match=message):
        config.get("trace")


def test_a_setting_left_in_the_environment_is_an_error_not_ignored(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL", "claude-haiku-4-5")
    with pytest.raises(ValueError, match="TEXT_MODEL → text_model"):
        config.get("text_model")


def test_the_text_model_comes_from_jev_toml(tmp_path, monkeypatch):
    from jev_ultrafast import model

    settings(tmp_path, 'text_model = "some/model"\ntext_model_base_url = "https://llm.test/v1"\n')
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    sent = []
    monkeypatch.setattr(model, "post_json", lambda url, key, body: sent.append((url, body["model"])) or {})
    model.complete_json("system", {}, {}, "Testing")
    assert sent == [("https://llm.test/v1/chat/completions", "some/model")]


SKILL = Skill(path="linkedin-dms/check", task="Read DMs.", url="https://www.linkedin.com/messaging/")
DECISION = {
    "choice": "DONE",
    "operation": "DONE",
    "target": None,
    "confidence": 0.9,
    "probabilities": {"DONE": 0.9},
    "raw_answers": {"operation": {"choice": "DONE"}},
    "request": {"state": {"page": {"text": "Katharine: Thanks for sending me your resume."}}},
    "model": "jev-1.13.0",
    "usage": {"input_tokens": 700},
    "latency_ms": 230,
    "elapsed_ms": 240,
}
RECORD = {
    "route": {"use_case": "linkedin-dms", "skill": "linkedin-dms/check", "answers": {"use_case": {}}},
    "page": {"url": "https://www.linkedin.com/messaging/", "title": "LinkedIn", "text": "Katharine: Thanks…"},
    "decisions": [DECISION],
    "history": [],
    "elements": [{"index": "1", "label": "Search"}],
    "status": "done",
    "report": {"entries": [{"name": "Katharine Tan", "when": "Sep 29", "text": None}]},
}


def written(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    path = cli.save_trace(SKILL, RECORD, mode)
    return path, (json.loads(path.read_text()) if path else None)


def test_full_trace_keeps_everything(tmp_path, monkeypatch):
    _, trace = written(tmp_path, monkeypatch, "full")
    assert trace["page"]["text"] and trace["decisions"][0]["request"] and trace["elements"]


def test_low_trace_keeps_the_steps_and_outcome_but_no_page_content(tmp_path, monkeypatch):
    path, trace = written(tmp_path, monkeypatch, "low")
    assert trace["status"] == "done" and trace["skill"]["path"] == "linkedin-dms/check"
    assert trace["decisions"] == [{k: DECISION[k] for k in cli.DECISION}]
    assert trace["page"] == {"url": "https://www.linkedin.com/messaging/", "title": "LinkedIn"}
    assert "elements" not in trace and "answers" not in trace["route"]
    # The report is the run's answer, so it stays.
    assert trace["report"]["entries"][0]["name"] == "Katharine Tan"
    assert "resume" not in path.read_text().replace("Katharine Tan", "")


def test_trace_off_writes_nothing(tmp_path, monkeypatch):
    path, _ = written(tmp_path, monkeypatch, "off")
    assert path is None and not (tmp_path / "artifacts").exists()


def test_the_trace_flag_overrides_jev_toml(tmp_path, monkeypatch, capsys):
    settings(tmp_path, 'trace = "off"\n')
    runs = []
    monkeypatch.setattr(cli, "load_environment", lambda: None)
    monkeypatch.setattr(cli, "run_browser", lambda skill, details, *args: runs.append(args[-1]) or 0)
    cli.main(["--skill", "linkedin-dms/check", "--trace", "low", "check", "my", "dms"])
    cli.main(["--skill", "linkedin-dms/check", "check", "my", "dms"])
    assert runs == ["low", None]  # None: run_browser falls back to jev.toml


def test_typed_settings_are_read_as_their_type(tmp_path):
    settings(tmp_path, "server = false\nserver_idle_minutes = 3\nrun_timeout_seconds = 30\n")
    assert config.get("server") is False and config.get("server_idle_minutes") == 3
    assert config.get("run_timeout_seconds") == 30


def test_a_list_setting_defaults_to_empty_and_reads_back_as_a_list(tmp_path):
    settings(tmp_path, "")
    assert config.get("telegram_allowed") == []  # nobody may drive jev from a chat until it is set
    settings(tmp_path, "telegram_allowed = [123456789, 5]\n")
    assert config.get("telegram_allowed") == [123456789, 5]


def test_console_without_a_terminal_answers_no_and_stops_at_the_deadline(monkeypatch):
    from jev_ultrafast import console

    terminal = console.Console()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("no terminal, so no question"))
    assert terminal.ask("Send? [y/N] ") is False
    terminal.check()  # no budget yet
    terminal.budget(0.001)
    import time

    time.sleep(0.01)
    with pytest.raises(console.Stopped, match="timeout"):
        terminal.check()
    terminal.budget(60)
    terminal.cancelled = True
    with pytest.raises(console.Stopped, match="cancelled"):
        terminal.check()
