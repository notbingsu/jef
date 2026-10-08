"""One pending question, kept for the next request only.

A run that stops needing something only you can say — `Need more detail: today only, or the last week?` — leaves that
question here, with the words that led to it. The next request is judged against it in the same TypeSafe request that
routes, so asking costs no extra round trip. If it is the answer, the skill that asked runs again with both halves of
the request, which is why an answer to an answer compounds: the slot holds the merged request, not the first one.

One slot, at most `memory_minutes` old and `CARRIES` deep. There is no session and no history: this is a one-deep
carry that expires, not a conversation. Requests run one at a time under the server, so nothing here locks.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

# How many times one question may be answered by another question's answer before the chain is dropped. A fourth
# carry almost always means the merged request is wrong rather than incomplete.
CARRIES = 3
# A merged request past this length is dropped rather than grown further.
LENGTH = 2000


class Unanswered(ValueError):
    """A run that stopped because it needs something only the user can say.

    A ValueError, so every existing caller keeps treating it as the failure it is; the question is what makes it
    worth keeping for one more request."""

    def __init__(self, question, message=None):
        super().__init__(message or question)
        self.question = question


def path():
    return Path("artifacts") / "memory.json"


def forget():
    path().unlink(missing_ok=True)


def remember(skill, request, question, carries=1):
    """Keep the question a run stopped on, with the request that led to it. `request` is whatever the run was given,
    already merged if this run was itself a follow-up, so the chain compounds."""
    if carries > CARRIES or len(request) > LENGTH:
        forget()
        return None
    slot = {
        "skill": skill,
        # Not "request": a low trace drops that key everywhere as a model request body, and these are your words.
        "earlier_request": request,
        "question": question,
        "carries": carries,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(slot, indent=2))
    file.chmod(0o600)  # it holds your words, like a trace
    return slot


def pending():
    """The slot, if there is one worth offering: not expired, not too deep, not too long. Anything else is removed,
    so a bad or stale slot can never be consulted twice."""
    file = path()
    if not file.is_file():
        return None
    try:
        slot = json.loads(file.read_text())
        fields = (slot["skill"], slot["earlier_request"], slot["question"])
        when = datetime.fromisoformat(slot["at"])
        carries = int(slot["carries"])
    except (ValueError, KeyError, TypeError, OSError):
        forget()
        return None
    if not all(isinstance(field, str) and field for field in fields):
        forget()
        return None
    stale = datetime.now(timezone.utc) - when > timedelta(minutes=config.get("memory_minutes"))
    if stale or carries > CARRIES or len(slot["earlier_request"]) > LENGTH:
        forget()
        return None
    return slot


def merge(slot, request):
    """Both halves of the request, in the order they were said. The skill's own helper reads them together; nothing
    here tries to rewrite them into one sentence."""
    return f"{slot['earlier_request']}\n{request}"
