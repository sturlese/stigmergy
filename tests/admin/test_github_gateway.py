"""The Actions gateway, offline — the injected-opener pattern `server.webhook`'s own tests use.
What must be true: exact endpoints, auth header present, no token in any raised message, reads
cached, mutations uncached and cache-invalidating, both failure shapes mapped to ActionsError."""
import json
import urllib.error

import pytest

from stigmergy.admin.github import ActionsError, ActionsGateway

TOKEN = "ghp_secret_value_that_must_never_leak"


class _Response:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, payloads=None, error=None):
        self.requests = []
        self._payloads = payloads or {}
        self._error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        for fragment, payload in self._payloads.items():
            if fragment in request.full_url:
                return _Response(payload)
        return _Response({})


def _gateway(opener, **kwargs):
    return ActionsGateway(TOKEN, "acme/stigmergy", opener=opener, **kwargs)


def test_workflows_hits_the_actions_endpoint_with_the_auth_header():
    opener = Opener({"/actions/workflows?": {
        "workflows": [{"id": 9, "name": "gardener", "path": ".github/workflows/gardener.yml",
                       "state": "active", "extra": "dropped"}]}})
    rows = _gateway(opener).workflows()
    assert rows == [{"id": 9, "name": "gardener", "path": ".github/workflows/gardener.yml",
                     "state": "active"}]
    request = opener.requests[0]
    assert request.full_url == ("https://api.github.com/repos/acme/stigmergy"
                                "/actions/workflows?per_page=100")
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


def test_runs_names_the_workflow_file_in_the_url():
    opener = Opener({"/runs?": {"workflow_runs": [
        {"id": 1, "status": "completed", "conclusion": None, "event": "workflow_dispatch",
         "created_at": "x", "updated_at": "y", "html_url": "z", "display_title": "t"}]}})
    rows = _gateway(opener).runs("gardener.yml", limit=5)
    assert rows[0]["conclusion"] == ""   # None normalized — the UI renders words, not null
    assert opener.requests[0].full_url.endswith("/actions/workflows/gardener.yml/runs?per_page=5")


def test_dispatch_posts_ref_and_inputs_and_invalidates_the_read_cache():
    opener = Opener({"/actions/workflows?": {"workflows": []}})
    gateway = _gateway(opener)
    gateway.workflows()
    gateway.workflows()
    assert len(opener.requests) == 1, "second read must come from the cache"
    gateway.dispatch("retention-purge.yml", inputs={"dry_run": "true"})
    dispatch = opener.requests[-1]
    assert dispatch.full_url.endswith("/actions/workflows/retention-purge.yml/dispatches")
    assert dispatch.get_method() == "POST"
    assert json.loads(dispatch.data) == {"ref": "main", "inputs": {"dry_run": "true"}}
    gateway.workflows()
    assert len(opener.requests) == 3, "a mutation must invalidate the cache"


def test_enable_and_disable_hit_their_verb_endpoints():
    opener = Opener()
    gateway = _gateway(opener)
    gateway.set_enabled("index-rebuild.yml", enabled=False)
    gateway.set_enabled("index-rebuild.yml", enabled=True)
    assert opener.requests[0].full_url.endswith("/actions/workflows/index-rebuild.yml/disable")
    assert opener.requests[1].full_url.endswith("/actions/workflows/index-rebuild.yml/enable")
    assert all(r.get_method() == "PUT" for r in opener.requests)


def test_an_http_error_maps_to_actions_error_with_the_status_and_no_token():
    error = urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    with pytest.raises(ActionsError) as caught:
        _gateway(Opener(error=error)).workflows()
    assert caught.value.status == 403
    assert "403" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_an_unreachable_github_maps_to_actions_error_status_zero():
    with pytest.raises(ActionsError) as caught:
        _gateway(Opener(error=OSError("boom"))).runs("gardener.yml")
    assert caught.value.status == 0
    assert "unreachable" in str(caught.value)


def test_the_cache_expires_on_its_clock():
    now = {"t": 0.0}
    opener = Opener({"/actions/workflows?": {"workflows": []}})
    gateway = _gateway(opener, cache_ttl_s=60, clock=lambda: now["t"])
    gateway.workflows()
    now["t"] = 59.0
    gateway.workflows()
    assert len(opener.requests) == 1
    now["t"] = 61.0
    gateway.workflows()
    assert len(opener.requests) == 2
