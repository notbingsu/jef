"""jev over Telegram, offline: a real Unix socket and a real server running a scripted request, with a fake Bot API
in place of the network. No skills, no model, no Google, no paid calls.

The bot's own polling loop is not what these drive; `Bot.accept` is, one update at a time, because that is where
every decision lives and a scripted loop would only add timing to the test.
"""

import socket
import threading
import time

import pytest
from test_server import home, serve  # noqa: F401  the fixture and the helper, as test_timeouts reuses test_agent

from jev_ultrafast import client, console, server, telegram
from jev_ultrafast.command import parse

ME = 4242
STRANGER = 99
GROUP = -1001


def message(text, who=ME, chat=None, kind="private"):
    return {
        "update_id": 1,
        "message": {"text": text, "chat": {"id": who if chat is None else chat, "type": kind}, "from": {"id": who}},
    }


def press(data, who=ME, chat=None, callback="cb1"):
    return {
        "update_id": 2,
        "callback_query": {
            "id": callback,
            "data": data,
            "from": {"id": who},
            "message": {"chat": {"id": who if chat is None else chat, "type": "private"}},
        },
    }


class FakeApi:
    """Records what a run would have sent. `send` hands back a message id, as Telegram does."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent, self.writes, self.answered = [], [], []
        self.ids = 0

    def latest(self):
        return 0

    def updates(self, offset, timeout=telegram.POLL):
        return []

    def send(self, chat, text, buttons=None):
        with self.lock:
            self.ids += 1
            self.sent.append({"text": text, "buttons": buttons, "message": self.ids})
            self.writes.append({"message": self.ids, "text": text})
            return self.ids

    def edit(self, chat, message, text):
        with self.lock:
            self.writes.append({"message": message, "text": text})

    def pressed(self, callback):
        self.answered.append(callback)

    def keyboards(self):
        return [m for m in self.sent if m["buttons"]]

    def pad(self):
        """The latest text of the run's output message: its first send, plus every edit after it."""
        plain = [m for m in self.sent if not m["buttons"]]
        assert plain, "the run sent no output"
        return [w["text"] for w in self.writes if w["message"] == plain[0]["message"]][-1]


def waitfor(get, what, limit=5):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if value := get():
            return value
        time.sleep(0.01)
    raise AssertionError(f"{what} never happened")


def settle(chat):
    waitfor(lambda: not chat.busy, "the run ending")


def bot(home, run, allowed=(ME,)):  # noqa: F811
    serve(home, run)
    api = FakeApi()
    return telegram.Bot(api, allowed), api


def test_a_message_becomes_a_run_with_the_words_as_details(home):  # noqa: F811
    seen = {}

    def run(argv):
        seen["argv"], seen["tty"] = argv, console.current().interactive
        console.say("route: calendar/update-event  (86%, 410 ms)")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("move the dentist to 5.30pm"))
    settle(talking.chats[ME])
    assert seen["argv"] == ["--background", "--", "move", "the", "dentist", "to", "5.30pm"]
    assert seen["tty"] is True  # without it the server denies every confirmation
    assert "calendar/update-event" in api.pad()


def test_chat_words_cannot_become_flags(home):  # noqa: F811
    seen = {}

    def run(argv):
        seen["args"] = parse(argv)
        return 0

    talking, _ = bot(home, run)
    talking.accept(message("--skill calendar/delete-event everything"))
    settle(talking.chats[ME])
    assert seen["args"].skill is None  # routing was not skipped
    assert seen["args"].request == ["--skill", "calendar/delete-event", "everything"]
    assert seen["args"].background is True


def test_a_question_becomes_a_yes_no_keyboard_and_a_press_answers_it(home):  # noqa: F811
    seen = {}

    def run(argv):
        seen["answer"] = console.ask("  Delete “Dentist”? [y/N] ")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("delete the dentist"))
    asked = waitfor(lambda: next(iter(api.keyboards()), None), "a keyboard")
    # The terminal's [y/N] is gone; the buttons are the gate, and the question is the message's own line.
    assert asked["text"] == "<b>Delete “Dentist”?</b>"
    yes, no = asked["buttons"]["inline_keyboard"][0]
    assert [yes["text"], no["text"]] == ["Yes", "No"]
    talking.accept(press(yes["callback_data"]))
    settle(talking.chats[ME])
    assert seen["answer"] is True
    assert api.answered == ["cb1"]
    assert any(w["message"] == asked["message"] and "Yes" in w["text"] for w in api.writes)


def test_each_question_gets_its_own_answer(home):  # noqa: F811
    seen = []

    def run(argv):
        seen.append(console.ask("  Create this event? [y/N] "))
        seen.append(console.ask("  Create another? [y/N] "))
        return 0

    talking, api = bot(home, run)
    talking.accept(message("two events"))
    for reply in ("n", "y"):
        asked = waitfor(lambda n=len(seen): api.keyboards()[n] if len(api.keyboards()) > n else None, "a keyboard")
        talking.accept(press(f"{reply}:{asked['buttons']['inline_keyboard'][0][0]['callback_data'].split(':')[1]}"))
        waitfor(lambda n=len(seen): len(seen) > n, "an answer")
    settle(talking.chats[ME])
    assert seen == [False, True]


def test_a_press_from_an_old_question_is_ignored(home):  # noqa: F811
    seen = {}

    def run(argv):
        seen["answer"] = console.ask("  Delete “Dentist”? [y/N] ")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("delete the dentist"))
    asked = waitfor(lambda: next(iter(api.keyboards()), None), "a keyboard")
    talking.accept(press("y:0"))  # a button from before this question
    time.sleep(0.2)
    assert "answer" not in seen, "a stale press answered the live question"
    talking.accept(press(asked["buttons"]["inline_keyboard"][0][0]["callback_data"]))
    settle(talking.chats[ME])
    assert seen["answer"] is True


def test_an_unanswered_question_is_a_no(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(telegram, "ASK_WAIT", 0.2)  # below the server's own ASK_TIMEOUT, as the real value is
    seen = {}

    def run(argv):
        seen["answer"] = console.ask("  Create this event? [y/N] ")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("an event"))
    settle(talking.chats[ME])
    assert seen["answer"] is False
    assert "treated as no" in api.pad()


def test_an_unlisted_id_runs_nothing_and_is_not_answered(home, capsys):  # noqa: F811
    talking, api = bot(home, lambda argv: pytest.fail("an unlisted id must not run anything"))
    talking.accept(message("delete everything", who=STRANGER))
    talking.accept(press("y:1", who=STRANGER))
    assert api.sent == [], "a stranger must not learn the bot is here"
    assert api.answered == []
    assert str(STRANGER) in capsys.readouterr().out


def test_a_group_message_runs_nothing_even_from_an_allowed_id(home, capsys):  # noqa: F811
    talking, api = bot(home, lambda argv: pytest.fail("a group must not run anything"))
    talking.accept(message("check my linkedin", who=ME, chat=GROUP, kind="group"))
    assert api.sent == []
    assert str(GROUP) in capsys.readouterr().out


def test_say_lines_coalesce_into_one_edited_message(home):  # noqa: F811
    def run(argv):
        for step in range(5):
            console.say(f"{step * 800:>6} ms  CLICK [{step}] Ada Lovelace  93%")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("reply to ada"))
    settle(talking.chats[ME])
    assert len([m for m in api.sent if not m["buttons"]]) == 1, "one message per run, edited as it goes"
    assert len(api.writes) < 5, "five lines must not be five Telegram calls"
    body = api.pad()
    assert all(f"CLICK [{step}]" in body for step in range(5))
    assert body.index("CLICK [0]") < body.index("CLICK [4]")


def test_output_is_escaped_not_sent_as_html(home):  # noqa: F811
    def run(argv):
        console.say("  Katharine <b>Tan</b> & co")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("check my linkedin"))
    settle(talking.chats[ME])
    body = api.pad()
    assert "&lt;b&gt;Tan&lt;/b&gt;" in body and "&amp; co" in body
    assert "<pre>" not in body  # a chat message, not a code box


def test_cancel_stops_the_run_at_its_next_safe_point(home):  # noqa: F811
    def run(argv):
        while True:
            console.check()
            time.sleep(0.01)

    talking, api = bot(home, run)
    talking.accept(message("check my linkedin"))
    chat = talking.chats[ME]
    waitfor(lambda: chat.stream is not None, "the run starting")
    assert chat.cancel() is True
    settle(chat)
    assert any("CANCELLED" in w["text"] or "cancel" in w["text"] for w in api.writes)


def test_a_second_request_in_one_chat_is_refused_not_queued(home):  # noqa: F811
    ran = []

    def run(argv):
        ran.append(argv)
        console.ask("  Create this event? [y/N] ")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("first request"))
    waitfor(lambda: api.keyboards(), "the first question")
    talking.accept(message("second request"))
    assert any("/cancel" in m["text"] for m in api.sent), "the refusal must say how to stop the first"
    talking.accept(press(api.keyboards()[0]["buttons"]["inline_keyboard"][0][1]["callback_data"]))
    settle(talking.chats[ME])
    assert len(ran) == 1


def test_a_restart_is_relayed_once_and_the_chat_sees_why(home, monkeypatch):  # noqa: F811
    replies = iter([[{"type": "restart"}], [{"type": "say", "text": "hi"}, {"type": "done", "exit": 0}]])

    def connect():
        ours, theirs = socket.socketpair()
        script = next(replies)

        def answer():
            stream = theirs.makefile("rw", encoding="utf-8")
            stream.readline()
            for reply in script:
                server.send(stream, reply)
            theirs.close()

        threading.Thread(target=answer, daemon=True).start()
        return ours

    monkeypatch.setattr(client, "connect", connect)
    api = FakeApi()
    talking = telegram.Bot(api, [ME])
    talking.accept(message("move the dentist"))
    settle(talking.chats[ME])
    body = api.pad()
    assert "starting a fresh one" in body and "hi" in body


# --- what a chat is shown ---------------------------------------------------------------------------------------

HOLDING = {
    "kind": "holding", "code": "US.NVDA", "name": "NVIDIA", "currency": "USD", "quantity": 9.0, "sellable": 9.0,
    "cost": 193.6, "price": 238.91, "value": 2150.18, "unrealized": 410.67, "percent": 23.57,
    "realized": 2.87, "today": -0.1,
}


def test_a_chat_is_shown_the_item_not_the_terminals_columns(home):  # noqa: F811
    def run(argv):
        console.say("moomoo/holdings → moomoo.holdings", item={"kind": "skill", "path": "moomoo/holdings"})
        console.say("  US.NVDA  NVIDIA  9  193.60  238.91  2,150.18  +410.67  23.57%", item=HOLDING)
        return 0

    talking, api = bot(home, run)
    talking.accept(message("what do I hold"))
    settle(talking.chats[ME])
    body = api.pad()
    assert "<b>Holdings</b>" in body and "<b>US.NVDA</b>" in body and "NVIDIA" in body
    assert "+410.67 (+23.6%)" in body and "🟢" in body
    # None of the terminal's spacing survives, and nothing is a code box.
    assert "193.60 → 238.91" in body and "  193.60  238.91" not in body and "<pre>" not in body


def test_a_chat_is_not_shown_how_the_run_decided(home):  # noqa: F811
    """Routing odds, the leaf it picked and each action it took are how a run got there, not what it found."""

    def run(argv):
        console.say("route: moomoo/holdings  (100%, 300 ms)", item={"kind": "route", "skill": "moomoo/holdings"})
        console.say("moomoo/holdings → moomoo.holdings", item={"kind": "skill", "path": "moomoo/holdings"})
        console.say("  1530 ms  CLICK [9] Send  97%", item={"kind": "step", "operation": "CLICK"})
        console.say("  USD  total 10.00  risk LEVEL3", item={"kind": "funds", "currency": "USD", "total": 10.0,
                                                             "value": 4.0, "cash": 6.0, "risk": "LEVEL3"})
        console.say("  done", item={"kind": "outcome", "status": "done", "text": "done"})
        return 0

    talking, api = bot(home, run)
    talking.accept(message("what do I hold"))
    settle(talking.chats[ME])
    body = api.pad()
    assert "100%" not in body and "300 ms" not in body and "CLICK" not in body and "97%" not in body
    assert "1530" not in body and "route:" not in body
    assert "<b>10.00 USD</b>" in body  # what it found is there


def test_the_trace_is_fine_print_at_the_foot_of_the_message(home):  # noqa: F811
    def run(argv):
        console.say("moomoo/holdings → moomoo.holdings", item={"kind": "skill", "path": "moomoo/holdings"})
        console.say("  US.NVDA …", item=HOLDING)
        console.say("  trace: artifacts/runs/moomoo/holdings/20261008T080351Z.json",
                    item={"kind": "trace", "path": "artifacts/runs/moomoo/holdings/20261008T080351Z.json"})
        return 0

    talking, api = bot(home, run)
    talking.accept(message("what do I hold"))
    settle(talking.chats[ME])
    body = api.pad()
    assert body.endswith("<i>artifacts/runs/moomoo/holdings/20261008T080351Z.json</i>")
    assert body.index("US.NVDA") < body.index("<i>artifacts")


def test_a_line_with_no_item_reaches_a_chat_as_a_sentence(home):  # noqa: F811
    def run(argv):
        console.say("  deleted.")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("delete the dentist"))
    settle(talking.chats[ME])
    assert api.pad() == "deleted."


def test_an_item_no_view_knows_is_left_out(home):  # noqa: F811
    """Rendering nothing is a valid answer, which is how a terminal's bookkeeping stays out without a list of it."""

    def run(argv):
        console.say("  some column header", item={"kind": "columns"})
        console.say("  something to say")
        return 0

    talking, api = bot(home, run)
    talking.accept(message("hello"))
    settle(talking.chats[ME])
    assert api.pad() == "something to say"


def test_a_terminal_console_ignores_the_item(capsys):
    """The guarantee the whole arrangement rests on: describing a line is additional, so a skill saying what its
    line is about can never change what the terminal prints."""
    wide = "  US.NVDA    NVIDIA     9   193.60   238.91   2,150.18   +410.67   23.57%"
    console.Console().say(wide, item=HOLDING)
    assert capsys.readouterr().out == wide + "\n"


@pytest.mark.parametrize(
    "path, title",
    [
        ("moomoo/holdings", telegram.Holdings),
        ("moomoo/position", telegram.Position),
        ("moomoo/activity", telegram.Activity),
        ("moomoo/accounts", telegram.Accounts),
        ("calendar/find-events", telegram.Events),
        ("calendar/browser/create-event", telegram.Events),
        ("linkedin-dms/check", telegram.Messages),
        ("something/new", telegram.View),
    ],
)
def test_a_leaf_gets_its_own_view_then_its_branchs_then_the_plain_one(path, title):
    assert type(telegram.viewing(path)) is title
