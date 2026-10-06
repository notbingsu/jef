"""Where a run talks to whoever asked for it: this terminal, a `jev` client of `jev serve`, a chat front end later.

Code never prints or reads input itself; it goes through the current console. `check()` is called only at safe points
(before a decision, an action, a model call, a confirmation or a mutation) and raises Stopped once the requester has
cancelled or the run is out of time. Nothing already sent is ever interrupted.
"""

import contextvars
import sys
import time


class Stopped(Exception):
    """The requester cancelled, or the run ran out of time. Raised only at a safe point."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Console:
    """The terminal. Without one, every question is answered no, so a gated action never runs unattended."""

    def __init__(self):
        self.deadline = None
        self.cancelled = False

    @property
    def interactive(self):
        return sys.stdin.isatty()

    def say(self, text=""):
        print(text, flush=True)

    def ask(self, prompt):
        return self.interactive and input(prompt).strip().lower() in {"y", "yes"}

    def budget(self, seconds):
        """Start a run's clock."""
        self.deadline = time.monotonic() + seconds
        self.cancelled = False

    def check(self):
        if self.cancelled:
            raise Stopped("cancelled")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise Stopped("timeout")


CURRENT = contextvars.ContextVar("console", default=Console())


def current():
    return CURRENT.get()


def say(text=""):
    current().say(text)


def ask(prompt):
    return current().ask(prompt)


def check():
    current().check()
