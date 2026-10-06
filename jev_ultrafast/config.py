"""jev.toml: jev's settings. None of them are secret; API keys stay in .env, which is git-ignored.

A missing file or key means the default. Unknown keys and values outside a key's choices are rejected, so a typo
fails loudly, as in skills/.
"""

import functools
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "trace": "full",
    "typesafe_model": "jev-latest",
    "text_model": "deepseek-chat",
    "text_model_base_url": "https://api.deepseek.com/v1",
    "text_model_reasoning": "low",
    # `jev` hands requests to a long-running `jev serve`, started on demand; --no-server runs one in-process.
    "server": True,
    "server_idle_minutes": 10,
    # A run stops at its next safe point after this long; nothing already sent is interrupted.
    "run_timeout_seconds": 180,
    # How long to wait for you to click Allow on Chrome's "Allow remote debugging?" prompt.
    "approval_wait_seconds": 120,
}
CHOICES = {"trace": ("full", "low", "off"), "text_model_reasoning": ("low", "none")}
# These were environment variables. One left in .env would now be ignored without a word, so it is an error instead.
MOVED = {
    "TYPESAFE_MODEL": "typesafe_model",
    "TEXT_MODEL": "text_model",
    "TEXT_MODEL_BASE_URL": "text_model_base_url",
    "TEXT_MODEL_REASONING": "text_model_reasoning",
}


def path():
    return Path.cwd() / "jev.toml"


@functools.cache
def read(file):
    if moved := [name for name in MOVED if name in os.environ]:
        raise ValueError(
            "Move these settings from .env to jev.toml: "
            + ", ".join(f"{name} → {MOVED[name]}" for name in moved)
            + ". Only API keys belong in .env."
        )
    try:
        data = tomllib.loads(file.read_text()) if file.is_file() else {}
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{file}: {error}") from None
    if unknown := set(data) - set(DEFAULTS):
        raise ValueError(f"{file}: unknown keys {sorted(unknown)}; allowed {sorted(DEFAULTS)}")
    for key, value in data.items():
        # The default's type is the setting's type; bool is checked exactly, since True is also an int.
        kind = type(DEFAULTS[key])
        if kind is bool and type(value) is not bool:
            raise ValueError(f"{file}: {key} must be true or false")
        if kind is int and (type(value) is not int or value <= 0):
            raise ValueError(f"{file}: {key} must be a positive whole number")
        if kind is str and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{file}: {key} must be a non-empty string")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{file}: {key} must be one of {', '.join(CHOICES[key])}")
    return {**DEFAULTS, **data}


def get(key):
    return read(path())[key]
