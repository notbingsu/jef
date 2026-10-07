"""jev over Telegram: a second client of the same `jev serve` the terminal uses, so a chat request gets the warm
imports, the warm Chrome connection and the one-at-a-time queue a terminal request gets.

One message is one run, and a run's question becomes two inline buttons, because `console.ask` returns only a bool.
There is no chat state machine: the run itself is the state, alive on the other end of the socket, so nothing here
has to be stored or resumed. Like `jev`, this process imports nothing heavy — no Chrome tooling and no model SDKs.
"""

import html
import os
import queue
import re
import threading
import time

import httpx

from . import client, command, config, server

ENDPOINT = "https://api.telegram.org/bot{}/{}"
POLL = 50  # how long getUpdates holds a request open, waiting for something to happen
EDIT_INTERVAL = 1.2  # Telegram takes about one message a second per chat
LENGTH = 3000  # a message caps at 4096 characters, with room for the <pre> wrapper and one more line
# Below server.ASK_TIMEOUT, so our answer is always what resolves a question. An answer the server is no longer
# waiting for would sit in its queue and silently become the next question's.
ASK_WAIT = 240
# The terminal's gate, which buttons replace. The gate itself lives in code, so this only drops the wording.
GATE = re.compile(r"\s*\[y/N\]\s*$")
WELCOME = (
    "Say what you want done, in plain words: “move the dentist to 5.30pm”, “any flights this month?”.\n\n"
    "A change is never made without a Yes. /help lists the skills, /cancel stops a run at its next safe point."
)


def log(text):
    print(f"{time.strftime('%H:%M:%S')}  telegram: {text}", flush=True)


def block(text):
    """A run's output, kept monospace: jev lines up columns with spaces, which Telegram otherwise collapses."""
    return f"<pre>{html.escape(text)}</pre>"


def keyboard(token):
    """Yes/No for one question. The token makes a button from an earlier question, or an earlier run, inert:
    `callback_data` stays in the chat's history forever."""
    return {
        "inline_keyboard": [
            [{"text": "Yes", "callback_data": f"y:{token}"}, {"text": "No", "callback_data": f"n:{token}"}]
        ]
    }


def argv(request):
    """What a chat message runs. `--background`, because nobody is watching a tab from a phone and a visible one
    would steal focus on the desktop; `--` so that a word beginning with a dash stays part of the request, since
    chat words must never be able to become flags like `--skill` and skip routing."""
    # TODO: forwarded-message context, as tele_gcal's _forwarded_context_by_user did. A forward and the instruction
    # about it are usually two messages, so it needs text accumulated across updates; not implemented.
    return ["--background", "--", *request.split()]


class Busy(RuntimeError):
    """A Bot API call that did not go through. `wait` is how long Telegram asked us to hold off."""

    def __init__(self, text, wait=1):
        super().__init__(text)
        self.wait = wait


class Api:
    """The Bot API methods a run needs. Injected, so a test drives a fake one. Everything passed in as `text` is
    already HTML: use block() for a run's output and html.escape() for anything else."""

    def __init__(self, token, http=None):
        self.token = token
        self.http = http or httpx.Client(timeout=POLL + 10)

    def call(self, method, **body):
        try:
            answer = self.http.post(ENDPOINT.format(self.token, method), json=body).json()
        except httpx.HTTPError as error:
            # The token is in every URL and httpx puts the URL in its messages, so never re-raise one as it came.
            raise Busy(f"{method}: {type(error).__name__}") from None
        if not answer.get("ok"):
            wait = answer.get("parameters", {}).get("retry_after", 1)
            raise Busy(f"{method}: {answer.get('description')}", wait)
        return answer["result"]

    def latest(self):
        """The next update_id to ask for. offset=-1 fetches only the newest, so a bot restarted after a day does
        not replay a day of queued messages at the owner's Chrome."""
        recent = self.call("getUpdates", offset=-1, timeout=0)
        return recent[-1]["update_id"] + 1 if recent else 0

    def updates(self, offset, timeout=POLL):
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])

    def send(self, chat, text, buttons=None):
        body = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
        if buttons:
            body["reply_markup"] = buttons
        return self.call("sendMessage", **body)["message_id"]

    def edit(self, chat, message, text):
        self.call("editMessageText", chat_id=chat, message_id=message, text=text, parse_mode="HTML")

    def pressed(self, callback):
        self.call("answerCallbackQuery", callback_query_id=callback)


class Pad:
    """The one Telegram message a run's output accumulates into. jev says a line per action and Telegram takes about
    one message a second, so lines are coalesced and the same message is edited rather than sent again."""

    def __init__(self, api, chat):
        self.api, self.chat = api, chat
        self.lock = threading.Lock()
        self.lines = []
        self.message = None  # the message being edited, or None before the first send
        self.shown = ""  # what that message holds, so an unchanged body is never sent again
        self.written = 0.0  # when it last went out; 0 makes the run's first line immediate
        self.timer = None

    def say(self, text):
        """Keep a line. It goes out now if the last write is old enough, and otherwise one timer carries it."""
        with self.lock:
            self.lines.append(text)
            due = time.monotonic() - self.written >= EDIT_INTERVAL
            self.disarm()
            if not due:
                # One timer, re-armed: a run that goes quiet right after a write must not sit on stale text.
                self.timer = threading.Timer(EDIT_INTERVAL, self.flush)
                self.timer.daemon = True
                self.timer.start()
        if due:
            self.flush()

    def disarm(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def flush(self):
        """Show what has been said. A failed call keeps the lines for the next try, so output is delayed, not lost."""
        with self.lock:
            self.disarm()
            body = "\n".join(self.lines)
            if not self.lines or body == self.shown:
                return
            try:
                if self.message is None:
                    self.message = self.api.send(self.chat, block(body))
                else:
                    self.api.edit(self.chat, self.message, block(body))
            except Busy as why:
                log(str(why))
                return
            self.shown, self.written = body, time.monotonic()
            if len(body) > LENGTH:
                # Over the message cap: this one is finished, and the rest of the run goes to a new message.
                self.lines, self.message, self.shown = [], None, ""

    def close(self):
        self.flush()
        with self.lock:
            if self.message is None:  # Telegram rejects an empty message, and a silent run still deserves a reply
                self.lines = ["the run said nothing."]
        self.flush()


class Chat:
    """One chat and the run it has in flight: the pad it writes into, the answer a button will give, and the
    connection a cancel goes down. No more state than that, because the run holds the rest."""

    def __init__(self, api, chat):
        self.api, self.chat = api, chat
        self.pad = Pad(api, chat)
        self.answers = queue.Queue()
        self.token = 0  # which question a press belongs to
        self.asked = None  # (message, question) while a keyboard is live
        self.stream = None
        self.sending = threading.Lock()
        self.busy = False

    def tell(self, text):
        """A line of the bot's own, outside any run's output."""
        try:
            self.api.send(self.chat, html.escape(text))
        except Busy as why:
            log(str(why))

    def start(self, request):
        """Run one request, relaying it into a fresh pad. Returns when the run has ended."""
        self.pad = Pad(self.api, self.chat)
        try:
            client.request(request, self.dispatch, tty=True, notice=self.pad.say)
        except (RuntimeError, OSError, ValueError) as error:
            self.pad.say(f"jev: {error}")
        finally:
            self.pad.close()
            self.stream, self.asked, self.busy = None, None, False

    def dispatch(self, stream):
        self.stream = stream
        return client.relay(stream, self.pad.say, self.ask, self.send)

    def send(self, stream, message):
        """Serialized: a cancel comes from the polling thread while the relay thread may be sending an answer."""
        with self.sending:
            server.send(stream, message)

    def ask(self, prompt):
        """A run's question, as two buttons. An unanswered one is a no, exactly as it is without a terminal."""
        self.pad.flush()  # the question must follow everything the run has said so far
        self.token += 1
        question = GATE.sub("", prompt).strip()
        try:
            self.asked = (self.api.send(self.chat, html.escape(question), keyboard(self.token)), question)
        except Busy as why:  # no keyboard reached the chat, so nobody can approve: a no changes nothing
            log(str(why))
            return False
        try:
            answer = self.answers.get(timeout=ASK_WAIT)
        except queue.Empty:
            self.pad.say("  no answer; treated as no")
            answer = False
        self.decided(answer)
        return answer

    def decided(self, answer):
        """Record the choice and take the keyboard away, so the chat shows what was decided and no button is
        pressed twice."""
        if self.asked is None:
            return
        (message, question), self.asked = self.asked, None
        try:
            self.api.edit(self.chat, message, f"{html.escape(question)} — <b>{'Yes' if answer else 'No'}</b>")
        except Busy as why:
            log(str(why))

    def answer(self, data):
        """A button press. One from an earlier question, or an earlier run, is ignored."""
        kind, _, token = data.partition(":")
        if self.asked is None or token != str(self.token):
            return
        self.answers.put(kind == "y")

    def cancel(self):
        """Stop the run at its next safe point. The socket is left open: closing it would cancel too, but throw
        away the lines the run still owes this chat."""
        if not self.busy or self.stream is None:
            return False
        self.send(self.stream, {"type": "cancel"})
        # Seeing that, the server answers its own pending question no, so release ours too or it waits ASK_WAIT for
        # nothing. The answer relay then sends is one the server is no longer waiting for, and it lands in this
        # run's own queue; harmless only because a cancelled run raises Stopped at the next console.check(), which
        # comes before every confirmation, so this run is never asked again.
        if self.asked is not None:
            self.answers.put(False)
        return True


class Bot:
    """Long polling: one blocking loop that takes messages and button presses, and a thread per run so the loop
    keeps answering while a run is going."""

    def __init__(self, api, allowed):
        self.api, self.allowed = api, set(allowed)
        self.chats = {}

    def run(self):
        offset = self.api.latest()
        log(f"listening; {len(self.allowed)} id(s) allowed")
        while True:
            try:
                updates = self.api.updates(offset)
            except Busy as why:
                log(str(why))
                time.sleep(why.wait)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.accept(update)
                except Exception as error:  # one bad update must not take the bot down
                    log(f"{error!r}")

    def permitted(self, chat, who):
        """Whose message may run anything. A chat drives the owner's logged-in Chrome and calendar, so refusal is
        the default: an id not in `telegram_allowed`, or any chat that is not that person's own private one, runs
        nothing and gets no reply — answering would only tell a stranger the bot is here. A group is refused even
        from an allowed id, since the output would go to everyone in it."""
        if who.get("id") in self.allowed and chat.get("id") == who.get("id") and chat.get("type") == "private":
            return True
        log(f"refused {who.get('id')} in {chat.get('type')} chat {chat.get('id')} (telegram_allowed in jev.toml)")
        return False

    def accept(self, update):
        if press := update.get("callback_query"):
            where = press.get("message", {}).get("chat", {})
            if not self.permitted(where, press.get("from", {})):
                return
            self.api.pressed(press["id"])
            if chat := self.chats.get(where["id"]):
                chat.answer(press.get("data", ""))
            return
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text or not self.permitted(message.get("chat", {}), message.get("from", {})):
            return
        where = message["chat"]["id"]
        if where not in self.chats:
            self.chats[where] = Chat(self.api, where)
        self.handle(self.chats[where], text)

    def handle(self, chat, text):
        name = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if name == "/cancel":
            chat.tell("cancelling at the next safe point" if chat.cancel() else "nothing is running")
            return
        if name == "/start":
            chat.tell(WELCOME)
            return
        if chat.busy:
            chat.tell("still working on the last request — /cancel to stop it, or wait.")
            return
        chat.busy = True
        request = ["--list"] if name == "/help" else argv(text)
        threading.Thread(target=chat.start, args=(request,), daemon=True).start()


def main():
    command.load_environment()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("jev-telegram: set TELEGRAM_BOT_TOKEN in .env (BotFather's /newbot gives you one)")
    # No allowed id means nobody could run anything, and the usual cause is the wrong directory: jev.toml, the
    # server's socket and skills/ are all found relative to it.
    if not (allowed := config.get("telegram_allowed")):
        raise SystemExit(
            "jev-telegram: set telegram_allowed in jev.toml to your numeric Telegram id (@userinfobot tells you "
            f"yours); none found in {config.path()}"
        )
    try:
        Bot(Api(token), allowed).run()
    except KeyboardInterrupt:
        log("stopped")
    return 0
