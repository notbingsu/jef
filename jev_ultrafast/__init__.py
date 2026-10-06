"""Jev chooses an observed action. Code owns execution."""

__all__ = ["Agent", "Browser"]


def __getattr__(name):
    # Lazy, so `jev` can reach its server without paying for the model SDKs on every call.
    if name == "Agent":
        from .agent import Agent

        return Agent
    if name == "Browser":
        from .browser import Browser

        return Browser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
