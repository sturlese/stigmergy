"""The GitHub Actions gateway — the crons tab's ONLY reach out of this process: exactly four
Actions endpoints (list workflows, list runs, dispatch, enable/disable); deploys, releases and
repo contents are out of scope.

`urllib` with an injectable `opener`, so tests drive the real request-building offline. Reads are
cached for `cache_ttl_s` (GitHub rate limits); mutations are never cached and invalidate the
cache. An `ActionsError` carries the endpoint and status — never the token, never a response body
echoed whole (a proxy's error page can be arbitrary HTML).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

_API = "https://api.github.com"
_TIMEOUT_S = 15
DEFAULT_CACHE_TTL_S = 60


class ActionsError(Exception):
    """A GitHub call failed. `status` is the HTTP status when there was one, 0 for a transport
    failure. The message is safe to hand to the UI verbatim."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


class ActionsGateway:
    """The real gateway. Tests either inject `opener` or replace the gateway whole — the service
    layer types against this class's four public methods and nothing else."""

    def __init__(self, token: str, repo: str, *, opener=None, cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
                 clock=time.monotonic):
        self._token = token
        self._repo = repo
        self._opener = opener or urllib.request.urlopen
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, object]] = {}

    # ── the four calls ────────────────────────────────────────────────────────────────────────
    def workflows(self) -> list[dict]:
        """Every workflow with its `state` (`active` / `disabled_manually` / ...). Cached."""
        data = self._cached("workflows", f"/repos/{self._repo}/actions/workflows?per_page=100")
        return [
            {"id": w.get("id"), "name": w.get("name", ""), "path": w.get("path", ""),
             "state": w.get("state", "")}
            for w in (data.get("workflows") or [])
        ]

    def runs(self, workflow_file: str, *, limit: int = 10) -> list[dict]:
        """The most recent runs of one workflow file, newest first. Cached per file."""
        per_page = max(1, min(int(limit), 50))
        data = self._cached(
            f"runs:{workflow_file}:{per_page}",
            f"/repos/{self._repo}/actions/workflows/{urllib.parse.quote(workflow_file)}/runs"
            f"?per_page={per_page}")
        return [
            {"id": r.get("id"), "status": r.get("status", ""),
             "conclusion": r.get("conclusion") or "",
             "event": r.get("event", ""), "created_at": r.get("created_at", ""),
             "updated_at": r.get("updated_at", ""), "html_url": r.get("html_url", ""),
             "display_title": r.get("display_title", "")}
            for r in (data.get("workflow_runs") or [])
        ]

    def dispatch(self, workflow_file: str, *, ref: str = "main", inputs: dict | None = None) -> None:
        """`workflow_dispatch` — the same act as `gh workflow run <file>`. 204 on success."""
        body = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        self._request(
            "POST",
            f"/repos/{self._repo}/actions/workflows/{urllib.parse.quote(workflow_file)}/dispatches",
            body=body)
        self._cache.clear()

    def set_enabled(self, workflow_file: str, *, enabled: bool) -> None:
        """Enable/disable the workflow."""
        verb = "enable" if enabled else "disable"
        self._request(
            "PUT",
            f"/repos/{self._repo}/actions/workflows/{urllib.parse.quote(workflow_file)}/{verb}")
        self._cache.clear()

    # ── plumbing ──────────────────────────────────────────────────────────────────────────────
    def _cached(self, key: str, path: str) -> dict:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._cache_ttl_s:
            return hit[1]
        data = self._request("GET", path)
        self._cache[key] = (now, data)
        return data

    def _request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            _API + path, data=payload, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "stigmergy-admin-console",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            })
        try:
            with self._opener(request, timeout=_TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as ex:
            # The status is the diagnosis — the body is never echoed (GitHub's, sometimes a
            # proxy's, and never needed to act).
            raise ActionsError(
                f"GitHub answered {ex.code} for {method} {path.split('?')[0]}",
                status=ex.code) from ex
        except OSError as ex:
            raise ActionsError(
                f"GitHub unreachable ({ex.__class__.__name__}) for {method} "
                f"{path.split('?')[0]}") from ex
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise ActionsError(
                f"GitHub returned an unparseable body for {method} {path.split('?')[0]}") from ex
