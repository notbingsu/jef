"""The `jev` side of `jev serve`: hand the request to this project's server, starting one when none answers, and relay
what it says and asks. Ctrl-C cancels the run at its next safe point; a second Ctrl-C leaves at once.
"""

import fcntl
import json
import socket
import subprocess
import sys
import time

from . import config, server

# A fresh server imports jev's model SDKs before it listens; on a cold machine that takes several seconds.
START_WAIT = 20
LOG_LIMIT = 1_000_000


def log_path():
    return server.home() / "server.log"


def attempt(path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(path))
        return connection
    except OSError:
        connection.close()
        return None


def draining():
    """True while a retiring server still holds the lock, so a fresh one is waiting to take over."""
    try:
        with open(server.home() / "lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(held, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def spawn():
    home = server.home()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    log = log_path()
    if log.exists() and log.stat().st_size > LOG_LIMIT:
        log.replace(log.with_suffix(".log.1"))
    with open(log, "a") as output:
        subprocess.Popen(
            [sys.executable, "-m", "jev_ultrafast.server"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # your Ctrl-C reaches this client, never the server
        )


def connect():
    """A connection to this project's server, starting one if none answers."""
    path = server.socket_path()
    if connection := attempt(path):
        return connection
    spawn()
    started = time.monotonic()
    # Waiting on a retiring server can take as long as the run it is finishing.
    limit = started + START_WAIT + config.get("run_timeout_seconds")
    told = False
    while time.monotonic() < limit:
        if connection := attempt(path):
            return connection
        # The new server holds the lock while it imports, so a held lock means "retiring" only after START_WAIT.
        if time.monotonic() - started > START_WAIT:
            if not draining():
                break
            if not told:
                print("  still waiting: a previous jev server is finishing its run", flush=True)
                told = True
        time.sleep(0.1)
    raise RuntimeError(f"the jev server did not start; see {log_path()}, or run with --no-server")


def run(argv):
    interactive = sys.stdin.isatty()
    for _ in range(2):  # a server running older code answers "restart" once; the next connection reaches a fresh one
        connection = connect()
        stream = connection.makefile("rw", encoding="utf-8")
        server.send(stream, {"type": "run", "argv": argv, "tty": interactive, "fingerprint": server.fingerprint()})
        try:
            result = relay(stream, interactive)
        finally:
            # The socket stays open while a file made from it is, so close both: the server sees us go at once.
            stream.close()
            connection.close()
        if result != "restart":
            return result
        print("  jev changed since its server started; starting a fresh one", flush=True)
    raise RuntimeError("the jev server kept asking for a restart; run with --no-server")


def relay(stream, interactive):
    cancelling = False
    while True:
        try:
            line = stream.readline()
            if not line:
                raise RuntimeError(f"the jev server stopped unexpectedly; see {log_path()}")
            message = json.loads(line)
            kind = message.get("type")
            if kind == "say":
                print(message["text"], flush=True)
            elif kind == "queued":
                ahead = message["ahead"]
                print(f"  queued behind {ahead} request{'s' if ahead > 1 else ''}", flush=True)
            elif kind == "ask":
                answer = interactive and input(message["prompt"]).strip().lower() in {"y", "yes"}
                server.send(stream, {"type": "answer", "value": answer})
            elif kind == "done":
                return message["exit"]
            elif kind == "restart":
                return "restart"
        except KeyboardInterrupt:
            if cancelling:
                raise
            cancelling = True
            print("\n  cancelling at the next safe point (Ctrl-C again to leave now)", flush=True)
            server.send(stream, {"type": "cancel"})


def stop():
    connection = attempt(server.socket_path())
    if connection is None:
        print("no jev server is running", flush=True)
        return 0
    with connection:
        stream = connection.makefile("rw", encoding="utf-8")
        server.send(stream, {"type": "stop"})
        stream.readline()
    print("the jev server exits once its current run, if any, ends", flush=True)
    return 0
