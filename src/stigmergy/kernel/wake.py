"""Wake the writer when work is queued for it.

The writer stops its own machine after a while with nothing to do. Whatever queues work for it
calls `writer()`, which starts a stopped writer machine through the Fly Machines API in the
background and never delays or fails the caller. A writer that is running but about to stop may
already have looked at the queue for the last time, so a running machine is watched for up to a
minute and started again if it stops in that time; a request made while a round runs gets a round
of its own. Without `STIGMERGY_FLY_API_TOKEN` and `FLY_APP_NAME`, or inside the writer itself, it
does nothing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TOKEN_ENV = "STIGMERGY_FLY_API_TOKEN"
API = "https://api.machines.dev/v1"
WRITER_GROUP = "worker"
STOP_WATCH_S = 60

_lock = threading.Lock()
_requested = False
_running = False


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(TOKEN_ENV, "").strip() and env.get("FLY_APP_NAME", "").strip()
                and env.get("FLY_PROCESS_GROUP", "") != WRITER_GROUP)


def writer(env=None) -> None:
    """Make sure the writer will see what was just queued."""
    global _requested, _running
    if not configured(env):
        return
    with _lock:
        _requested = True
        if _running:
            return
        _running = True
    threading.Thread(target=_rounds, args=(dict(os.environ if env is None else env),),
                     name="wake-writer", daemon=True).start()


def _rounds(env: dict) -> None:
    global _requested, _running
    while True:
        with _lock:
            if not _requested:
                _running = False
                return
            _requested = False
        try:
            wake_round(env)
        except Exception as error:  # noqa: BLE001 — waking is best effort; the next request retries
            log.warning("could not wake the writer (%s)", error.__class__.__name__)


def _call(env: dict, method: str, path: str, *, opener=None, timeout: float = 15):
    token = env[TOKEN_ENV].strip()
    request = urllib.request.Request(
        f"{API}/apps/{env['FLY_APP_NAME']}{path}", method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Authorization": token if token.startswith("FlyV1 ") else f"Bearer {token}",
                 "Content-Type": "application/json"})
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def wake_round(env: dict, *, opener=None) -> list[str]:
    """Start every stopped writer machine, and one that stops within the watch; what it did."""
    actions = []
    for machine in _call(env, "GET", "/machines", opener=opener):
        if (machine.get("config") or {}).get("metadata", {}).get("fly_process_group") != WRITER_GROUP:
            continue
        machine_id, state = machine["id"], machine.get("state")
        if state in ("stopped", "suspended"):
            _call(env, "POST", f"/machines/{machine_id}/start", opener=opener)
            actions.append(f"started {machine_id}")
        elif state in ("started", "starting", "stopping"):
            try:
                _call(env, "GET", f"/machines/{machine_id}/wait?state=stopped&timeout={STOP_WATCH_S}",
                      opener=opener, timeout=STOP_WATCH_S + 15)
            except urllib.error.HTTPError as error:
                if error.code == 408:   # still running at the end of the watch: it will see the work
                    actions.append(f"running {machine_id}")
                    continue
                raise
            _call(env, "POST", f"/machines/{machine_id}/start", opener=opener)
            actions.append(f"restarted {machine_id}")
    return actions
