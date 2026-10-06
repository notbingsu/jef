"""jev serve and its client over a real Unix socket. Offline: the server runs a scripted request, never jev's skills."""

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jev_ultrafast import client, console, server

RUNNING = []


@pytest.fixture
def home(monkeypatch):
    # pytest's tmp_path is too long for a macOS socket path, so the socket lives under a short /tmp folder.
    folder = Path(tempfile.mkdtemp(prefix="jev-", dir="/tmp"))
    monkeypatch.setattr(server, "home", lambda: folder)
    monkeypatch.setattr(client, "spawn", lambda: pytest.fail("a running server must be reused, not respawned"))
    yield folder
    while RUNNING:  # no server outlives its test
        started = RUNNING.pop()
        started.retire("test over")
        started.thread.join(timeout=5)
    shutil.rmtree(folder, ignore_errors=True)


def serve(home, run, idle=30.0):
    """Start a server in a thread; returns it once it listens."""
    started = server.Server(home / "jev.sock", idle, run=run)
    RUNNING.append(started)
    thread = threading.Thread(target=started.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.reachable(started.path):
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    started.thread = thread
    return started


def open_request(path, argv, fingerprint=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    stream = connection.makefile("rw", encoding="utf-8")
    first = {"type": "run", "argv": argv, "tty": True, "fingerprint": fingerprint or server.fingerprint()}
    server.send(stream, first)
    return connection, stream


def messages(stream, until="done"):
    seen = []
    for line in stream:
        seen.append(json.loads(line))
        if seen[-1]["type"] in {until, "done", "restart"}:
            break
    return seen


def test_say_and_ask_round_trip_through_the_client(home, monkeypatch, capsys):
    def run(argv):
        console.say(f"running {argv}")
        return 0 if console.ask("Send? [y/N] ") else 1

    serve(home, run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert client.run(["check", "dms"]) == 0
    assert "running ['check', 'dms']" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert client.run(["check", "dms"]) == 1


def test_requests_run_one_at_a_time_in_order(home):
    release, order = threading.Event(), []

    def run(argv):
        order.append(argv[0])
        if argv[0] == "first":
            release.wait(5)
        return 0

    started = serve(home, run)
    first, first_stream = open_request(started.path, ["first"])
    while not order:
        time.sleep(0.01)
    second, second_stream = open_request(started.path, ["second"])
    assert messages(second_stream, until="queued")[0] == {"type": "queued", "ahead": 1}
    assert order == ["first"]
    release.set()
    assert messages(first_stream)[-1] == {"type": "done", "exit": 0}
    assert messages(second_stream)[-1] == {"type": "done", "exit": 0}
    assert order == ["first", "second"]


def test_cancel_drops_a_queued_request_and_stops_a_running_one_at_a_safe_point(home):
    release, ran = threading.Event(), []

    def run(argv):
        ran.append(argv[0])
        while True:
            try:
                console.check()
            except console.Stopped:
                return 130
            if argv[0] == "first" and release.is_set():
                return 0
            time.sleep(0.01)

    started = serve(home, run)
    first, first_stream = open_request(started.path, ["first"])
    while not ran:
        time.sleep(0.01)
    queued, queued_stream = open_request(started.path, ["queued"])
    messages(queued_stream, until="queued")
    server.send(queued_stream, {"type": "cancel"})
    server.send(first_stream, {"type": "cancel"})
    assert messages(first_stream)[-1] == {"type": "done", "exit": 130}
    assert messages(queued_stream)[-1] == {"type": "done", "exit": 130}
    assert ran == ["first"]  # the cancelled request never started


def test_a_client_that_goes_away_answers_no(home):
    answers = []

    def run(argv):
        answers.append(console.ask("Send? [y/N] "))
        return 0

    started = serve(home, run)
    connection, stream = open_request(started.path, ["reply"])
    assert messages(stream, until="ask")[-1]["type"] == "ask"
    stream.close()
    connection.close()
    deadline = time.monotonic() + 5
    while not answers:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert answers == [False]


def test_an_idle_server_exits_and_removes_its_socket(home):
    started = serve(home, lambda argv: 0, idle=0.3)
    started.thread.join(timeout=5)
    assert not started.thread.is_alive() and not started.path.exists()


def test_a_server_running_old_code_asks_for_a_restart_and_retires(home):
    started = serve(home, lambda argv: pytest.fail("a stale server must not run anything"))
    connection, stream = open_request(started.path, ["check"], fingerprint="older")
    assert messages(stream) == [{"type": "restart"}]
    started.thread.join(timeout=5)
    assert not started.thread.is_alive() and not started.path.exists()


def test_the_client_restarts_a_stale_server_once(home, monkeypatch, capsys):
    replies = iter([[{"type": "restart"}], [{"type": "say", "text": "hi"}, {"type": "done", "exit": 0}]])

    def connect():
        ours, theirs = socket.socketpair()
        script = next(replies)

        def answer():
            stream = theirs.makefile("rw", encoding="utf-8")
            stream.readline()
            for message in script:
                server.send(stream, message)
            theirs.close()

        threading.Thread(target=answer, daemon=True).start()
        return ours

    monkeypatch.setattr(client, "connect", connect)
    assert client.run(["check"]) == 0
    assert "starting a fresh one" in capsys.readouterr().out


def test_stop_retires_the_server(home, capsys):
    started = serve(home, lambda argv: 0)
    assert client.stop() == 0
    started.thread.join(timeout=5)
    assert not started.thread.is_alive()
    assert client.stop() == 0 and "no jev server is running" in capsys.readouterr().out


def test_only_one_server_holds_the_lock(home):
    held = server.lock(wait=0)
    assert held is not None
    try:
        assert server.lock(wait=0.3) is None
    finally:
        held.close()
    again = server.lock(wait=0)
    assert again is not None
    again.close()


def test_a_bug_in_one_request_does_not_take_the_queue_down(home):
    def run(argv):
        if argv[0] == "bad":
            raise KeyError("boom")
        return 0

    started = serve(home, run)
    bad, bad_stream = open_request(started.path, ["bad"])
    said = messages(bad_stream)
    assert said[-1] == {"type": "done", "exit": 1} and "internal error" in said[0]["text"]
    good, good_stream = open_request(started.path, ["good"])
    assert messages(good_stream)[-1] == {"type": "done", "exit": 0}
