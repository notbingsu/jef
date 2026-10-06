"""Every test sees jev's default settings, never the developer's jev.toml or shell: a personal text_model must not
turn an offline test into a paid API call."""

import pytest

from jev_ultrafast import config


@pytest.fixture(autouse=True)
def default_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "path", lambda: tmp_path / "jev.toml")
    for name in config.MOVED:
        monkeypatch.delenv(name, raising=False)
    config.read.cache_clear()
    yield
    config.read.cache_clear()
