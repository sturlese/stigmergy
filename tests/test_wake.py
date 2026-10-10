"""The writer is woken through the Fly Machines API whenever work is queued for it, and a writer
caught stopping within the watch is started again."""
import io
import json
import threading
import urllib.error

from stigmergy.kernel import wake

ENV = {wake.TOKEN_ENV: "FlyV1 fm2_secret", "FLY_APP_NAME": "stigmergy", "FLY_PROCESS_GROUP": "app"}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _machines(*states):
    return [{"id": "app1", "state": "started", "config": {"metadata": {"fly_process_group": "app"}}},
            *({"id": f"w{i}", "state": state, "config": {"metadata": {"fly_process_group": "worker"}}}
              for i, state in enumerate(states))]


def _opener(machines, *, wait_code=None):
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url.split("/apps/stigmergy")[1],
                      request.get_header("Authorization")))
        if request.full_url.endswith("/machines"):
            return _Response(json.dumps(machines).encode())
        if "/wait?" in request.full_url and wait_code is not None:
            raise urllib.error.HTTPError(request.full_url, wait_code, "timeout", {}, None)
        return _Response(b"{}")

    return opener, calls


def test_a_stopped_writer_is_started_and_the_app_is_left_alone():
    opener, calls = _opener(_machines("stopped"))
    assert wake.wake_round(ENV, opener=opener) == ["started w0"]
    assert [(method, path) for method, path, _auth in calls] == [
        ("GET", "/machines"), ("POST", "/machines/w0/start")]
    assert all(auth == "FlyV1 fm2_secret" for _m, _p, auth in calls)


def test_a_writer_that_stops_within_the_watch_is_started_again():
    opener, calls = _opener(_machines("started"))
    assert wake.wake_round(ENV, opener=opener) == ["restarted w0"]
    assert ("GET", f"/machines/w0/wait?state=stopped&timeout={wake.STOP_WATCH_S}") in \
        [(method, path) for method, path, _auth in calls]


def test_a_writer_still_running_after_the_watch_is_left_running():
    opener, calls = _opener(_machines("started"), wait_code=408)
    assert wake.wake_round(ENV, opener=opener) == ["running w0"]
    assert not any(method == "POST" for method, _path, _auth in calls)


def test_a_plain_token_is_sent_as_a_bearer_token():
    opener, calls = _opener(_machines("stopped"))
    wake.wake_round({**ENV, wake.TOKEN_ENV: "plain"}, opener=opener)
    assert calls[0][2] == "Bearer plain"


def test_nothing_happens_without_the_token_or_inside_the_writer(monkeypatch):
    started = []
    monkeypatch.setattr(wake.threading, "Thread", lambda *a, **k: started.append(k) or threading.Thread())
    wake.writer({"FLY_APP_NAME": "stigmergy"})
    wake.writer({**ENV, "FLY_PROCESS_GROUP": "worker"})
    assert started == []
    assert not wake.configured({})


def test_a_request_made_during_a_round_gets_a_round_of_its_own(monkeypatch):
    rounds = []

    def round_(env):
        rounds.append(env)
        if len(rounds) == 1:
            wake.writer(ENV)      # queued while the first round runs

    monkeypatch.setattr(wake, "wake_round", round_)
    monkeypatch.setattr(wake.threading, "Thread",
                        lambda target, args, **_k: type("T", (), {"start": lambda self: target(*args)})())
    wake.writer(ENV)
    assert len(rounds) == 2
    assert wake._running is False and wake._requested is False
