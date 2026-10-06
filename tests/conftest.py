"""Every test sees jev's default settings, never the developer's jev.toml or shell: a personal text_model must not
turn an offline test into a paid API call."""

import pytest

from jev_ultrafast import browser, config, console


@pytest.fixture(autouse=True)
def default_settings(tmp_path, monkeypatch):
    # Defaults, except that `jev` runs in-process: only the server's own tests start a server.
    # In a folder of its own: some tests use tmp_path itself as a skill tree.
    settings = tmp_path / ".jev" / "jev.toml"  # a dot folder is never a skill branch
    settings.parent.mkdir()
    settings.write_text("server = false\n")
    monkeypatch.setattr(config, "path", lambda: settings)
    monkeypatch.setattr(browser, "TABS", None)  # warm tabs exist only inside a server's own test
    for name in config.MOVED:
        monkeypatch.delenv(name, raising=False)
    config.read.cache_clear()
    # A test that ran cli.main started the shared console's clock; no test may inherit it.
    terminal = console.current()
    terminal.deadline, terminal.cancelled = None, False
    yield
    config.read.cache_clear()
    terminal.deadline, terminal.cancelled = None, False
