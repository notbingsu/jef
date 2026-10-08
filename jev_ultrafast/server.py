"""jev serve: one long-running jev per project, so imports, HTTP connections, the Chrome connection and warm tabs
outlive a single request.

`jev …` is a thin client (client.py) that starts this on demand. Requests arrive on a Unix socket only you can open,
and run one at a time in arrival order. Each request gets a console bound to its connection: what it says streams
back, what it asks waits for the client's answer, and a cancelled or disconnected client stops it at its next safe
point, answering no to anything pending. The server exits after `server_idle_minutes` with nothing to do, and retires
as soon as the code, jev.toml or .env it started with has changed, after finishing what it already accepted.
"""

import fcntl
import hashlib
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path

from . import config, console

PACKAGE = Path(__file__).parent
# An unanswered question is a no after this long, so a forgotten prompt never holds the queue.
ASK_TIMEOUT = 300


def home():
    return Path.cwd() / "artifacts" / "server"


def socket_path():
    path = home() / "jev.sock"
    # macOS caps a socket path at 104 bytes; a deep checkout falls back to a short per-user name.
    if len(os.fsencode(path)) < 100:
        return path
    return Path("/tmp") / f"jev-{os.getuid()}-{hashlib.sha256(bytes(path)).hexdigest()[:12]}.sock"


def fingerprint():
    """What the server must match to answer a client: jev's source, jev.toml and .env as they are now."""
    digest = hashlib.sha256()
    for file in sorted(PACKAGE.iterdir()):
        if file.suffix in {".py", ".js"}:
            digest.update(file.name.encode() + file.read_bytes())
    for file in (config.path(), Path.cwd() / ".env"):
        digest.update(str(file.stat().st_mtime_ns if file.exists() else "-").encode())
    return digest.hexdigest()[:16]


def send(stream, message):
    stream.write(json.dumps(message) + "\n")
    stream.flush()


class Job(console.Console):
    """One request, and the console it runs under. The connection's reader thread answers and cancels it."""

    def __init__(self, stream, argv, tty):
        super().__init__()
        self.stream, self.argv, self.tty = stream, argv, tty
        self.answers = queue.Queue()
        self.connected = True
        self.lock = threading.Lock()

    @property
    def interactive(self):
        return self.tty and self.connected

    def emit(self, message):
        with self.lock:
            if not self.connected:
                return
            try:
                send(self.stream, message)
            except (OSError, ValueError):
                self.disconnect()

    def say(self, text="", item=None):
        self.emit({"type": "say", "text": text, **({"item": item} if item is not None else {})})

    def ask(self, prompt):
        if not self.interactive:
            return False
        self.emit({"type": "ask", "prompt": prompt})
        try:
            return bool(self.answers.get(timeout=ASK_TIMEOUT))
        except queue.Empty:
            self.say("  no answer; treated as no")
            return False

    def disconnect(self):
        # Gone means cancelled, and any question still waiting is a no.
        self.connected = False
        self.cancelled = True
        self.answers.put(False)


class Server:
    def __init__(self, path, idle_seconds, run=None):
        self.path, self.idle_seconds = path, idle_seconds
        self.run = run or execute
        self.fingerprint = fingerprint()
        self.jobs = queue.Queue()
        self.waiting = []  # jobs accepted and not finished, oldest first
        self.retiring = False
        self.last_active = time.monotonic()
        self.listener = None

    def log(self, text):
        print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)

    def serve(self):
        from . import browser  # here, so a client importing this module never loads Chrome's tooling

        browser.TABS = browser.Tabs()
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.listener.listen()
        self.listener.settimeout(0.2)
        worker = threading.Thread(target=self.work, daemon=True)
        worker.start()
        self.log(f"serving on {self.path} (pid {os.getpid()}, {self.fingerprint})")
        while not self.finished():
            listener = self.listener
            if listener is None:  # retiring: finish the queue, take nothing new
                time.sleep(0.2)
                continue
            try:
                connection, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
        self.stop_listening()
        self.jobs.put(None)
        worker.join(timeout=5)
        # Warm tabs are background tabs you never saw; they go with the server.
        browser.TABS.close_all()
        browser.TABS = None
        self.log("stopped")

    def finished(self):
        idle = not self.waiting and time.monotonic() - self.last_active > self.idle_seconds
        if idle and not self.retiring:
            self.log(f"idle for {self.idle_seconds:.0f} s; exiting")
        return not self.waiting and (self.retiring or idle)

    def stop_listening(self):
        # A retiring server takes no new requests; the next client starts a fresh server, which waits for this
        # one's lock before it serves.
        if self.listener:
            self.listener.close()
            self.listener = None
            self.path.unlink(missing_ok=True)

    def retire(self, why):
        if not self.retiring:
            self.log(f"retiring: {why}")
        self.retiring = True
        self.stop_listening()

    def handle(self, connection):
        stream = connection.makefile("rw", encoding="utf-8")
        try:
            first = json.loads(stream.readline() or "null")
        except ValueError:
            first = None
        if not isinstance(first, dict):
            stream.close()
            connection.close()
            return
        self.last_active = time.monotonic()
        if first.get("type") == "stop":
            self.retire("asked to stop")
            send(stream, {"type": "done", "exit": 0})
            stream.close()
            connection.close()
            return
        if first.get("fingerprint") != self.fingerprint:
            send(stream, {"type": "restart"})
            stream.close()
            connection.close()
            self.retire("jev, jev.toml or .env changed")
            return
        job = Job(stream, list(first.get("argv", [])), bool(first.get("tty")))
        self.waiting.append(job)
        self.jobs.put(job)
        ahead = len(self.waiting) - 1
        if ahead:
            job.emit({"type": "queued", "ahead": ahead})
        # This thread now only listens: answers go to the job, a cancel or a closed connection stops it.
        for line in stream:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("type") == "answer":
                job.answers.put(bool(message.get("value")))
            elif message.get("type") == "cancel":
                job.cancelled = True
                job.answers.put(False)
        job.disconnect()
        connection.close()

    def work(self):
        while (job := self.jobs.get()) is not None:
            if job.cancelled:
                self.finish(job, 130, "cancelled while queued")
                continue
            started = time.monotonic()
            token = console.CURRENT.set(job)
            try:
                code = self.run(job.argv)
            except Exception as error:  # a bug must not take the queue down with it
                self.log(f"error in {job.argv}: {error!r}")
                job.say(f"jev: internal error: {error}; see {home() / 'server.log'}")
                code = 1
            finally:
                console.CURRENT.reset(token)
            self.finish(job, code, f"{' '.join(job.argv)!r} exited {code} in {time.monotonic() - started:.1f} s")

    def finish(self, job, code, note):
        job.emit({"type": "done", "exit": code})
        self.waiting.remove(job)
        self.last_active = time.monotonic()
        self.log(note)


def execute(argv):
    from . import cli

    try:
        return cli.execute(cli.parse(argv))
    except SystemExit as stop:  # argparse; the client validated first, so this is a version mismatch at most
        return stop.code if isinstance(stop.code, int) else 2


def lock(wait):
    """Hold the project's server lock, waiting up to `wait` seconds for a retiring server to let go.
    Returns the open lock file, or None when another server is already serving."""
    home().mkdir(parents=True, exist_ok=True)
    os.chmod(home(), 0o700)
    held = open(home() / "lock", "w")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return held
        except BlockingIOError:
            if reachable(socket_path()) or time.monotonic() > deadline:
                held.close()
                return None
            time.sleep(0.2)


def reachable(path):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main():
    from .cli import load_environment

    load_environment()
    held = lock(wait=config.get("run_timeout_seconds") + 60)
    if held is None:
        return 0
    try:
        Server(socket_path(), config.get("server_idle_minutes") * 60).serve()
    finally:
        held.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
