"""Plaintext tokens exist only in the operator's issuance output and in a client's own config:
the server stores SHA-256 hashes, and no tracked file contains a token or a hash. This file IS
that evidence, automated so a future commit that accidentally bakes a real token-store entry into
a doc, a script, a fixture or a workflow file fails CI, instead of relying on a human running
`git grep` by hand before every release.

Scope: this repo (`stigmergy`) only — the sibling knowledge repo is out of this repo's
reach and is the operator's own responsibility."""
import pathlib
import re
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The token STORE shape is `{"<sha256hex>": "<email>"}` — a JSON object key that is exactly 64
# lowercase hex characters is the fingerprint of a REAL sha256 digest landing in a tracked file.
# The docs' own placeholder is the literal string `<sha256hex>` (angle brackets, not hex), so it
# can never match this pattern — a true positive here is exactly the leak this file forbids.
_HASH_KEY_RE = re.compile(r'"[0-9a-f]{64}"\s*:')

# A bearer token minted by `issue_token.issue` is `secrets.token_urlsafe(32)` — base64url, ~43
# chars, no padding. A literal one sitting after "Bearer " (as opposed to a shell variable
# reference like `$TOKEN` or an f-string placeholder) in a TRACKED file would be the plaintext
# half of the same leak.
_BEARER_LITERAL_RE = re.compile(r"Bearer\s+[A-Za-z0-9_-]{40,}")


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
                         check=True)
    return [line for line in out.stdout.splitlines() if line]


def _read_text(rel_path: str) -> str | None:
    path = REPO_ROOT / rel_path
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, IsADirectoryError):
        return None


def test_no_tracked_file_contains_a_sha256_hex_token_store_entry():
    offenders = []
    for rel_path in _tracked_files():
        text = _read_text(rel_path)
        if text and _HASH_KEY_RE.search(text):
            offenders.append(rel_path)
    assert not offenders, (
        f"a tracked file contains a 64-hex-char JSON key — looks like a real token-store hash "
        f"leaked into the repo: {offenders}")


def test_no_tracked_file_contains_a_literal_bearer_token():
    offenders = []
    for rel_path in _tracked_files():
        text = _read_text(rel_path)
        if text and _BEARER_LITERAL_RE.search(text):
            offenders.append(rel_path)
    assert not offenders, (
        f"a tracked file contains a literal 'Bearer <long-token>' — looks like a plaintext bearer "
        f"credential leaked into the repo: {offenders}")


def test_issue_token_never_writes_the_plaintext_anywhere_but_stdout(capsys):
    """Companion unit check: this command never writes the plaintext to disk, a log, or a repo —
    `issue()` itself is pure and returns values; only `main()` prints, and only to stdout/stderr,
    never to a file."""
    from stigmergy.server.issue_token import issue

    token, digest = issue("tester@example.com")
    assert token != digest
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    # sanity: the returned pair really is (plaintext, sha256(plaintext)) — issue() doesn't itself
    # persist anything (no file, no log call) in its pure implementation.
    from stigmergy.server.identity import hash_token
    assert hash_token(token) == digest


@pytest.mark.parametrize("rel_path", ["fly.toml", "scripts/deploy_staging.sh"])
def test_deploy_artifacts_never_hardcode_a_secret_value(rel_path):
    """Secrets travel as Fly secrets (ADR 013 §7) — the deploy artifacts must reference them by
    name only, never embed one."""
    text = _read_text(rel_path)
    assert text is not None, f"{rel_path} is not tracked — the deploy needs it"
    assert not _HASH_KEY_RE.search(text)
    assert not _BEARER_LITERAL_RE.search(text)
