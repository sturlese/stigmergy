"""`stigmergy-librarian-credential` — the git credential helper backed by the GitHub App.

The property under test is narrow and load-bearing: this helper prints an installation token with
`contents:write` on the knowledge repo, on stdout, where git takes whatever it finds AS the
credential. So it must answer for exactly one origin and print nothing at all otherwise.
"""
import io

import pytest

from stigmergy.librarian import gitcredential


# ── the helper answers for ONE origin ──────────────────────────────────────────────────────────
# `bootstrap.credential_scope` already scopes this to github.com in git's own config, so git never
# invokes it for anything else. That is a property of the CALLER. This is a console script on
# `PATH` inside the worker image: anything running as the worker's uid can execute it directly and
# read a fresh `contents:write` installation token off stdout. Found in a pre-publication audit,
# where the drained-and-ignored request was called out as the thing to correct — the fields DO
# change the answer.
@pytest.mark.parametrize("request_text", [
    "protocol=https\nhost=evil.example\n\n",
    "protocol=http\nhost=github.com\n\n",          # downgrade: the token would cross in clear
    "protocol=https\nhost=github.com.evil.example\n\n",   # suffix confusion
    "protocol=https\nhost=notgithub.com\n\n",
    "",                                             # no request at all
])
def test_the_helper_mints_nothing_for_an_origin_it_does_not_serve(request_text, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(request_text))
    minted = []
    monkeypatch.setattr(gitcredential, "credential_lines",
                        lambda *a, **k: minted.append(1) or ["username=x", "password=ghs_leaked"])

    rc = gitcredential.main(["get"])

    out, err = capsys.readouterr()
    assert rc == 1
    assert out == "", "a helper's STDOUT is taken by git AS the credential — it must stay empty"
    assert not minted, "the token was minted before the origin was checked"
    assert "refusing to mint" in err


def test_the_helper_still_answers_for_the_origin_it_does_serve(monkeypatch, capsys):
    """The benign twin. A check that only proves the refusal fires measures its sensitivity and
    never its specificity — and this one sits in the path every fetch the worker makes."""
    monkeypatch.setattr("sys.stdin", io.StringIO("protocol=https\nhost=github.com\npath=a/b\n\n"))
    monkeypatch.setattr(gitcredential, "credential_lines",
                        lambda *a, **k: ["username=x-access-token", "password=ghs_minted"])

    rc = gitcredential.main(["get"])

    out, _ = capsys.readouterr()
    assert rc == 0
    assert "password=ghs_minted" in out


def test_an_unknown_field_in_the_request_is_ignored_rather_than_refused():
    """git is free to add fields; a helper that refused on one it did not recognise would break on
    a git upgrade, in the fetch that runs before every claim."""
    fields = gitcredential._request_fields(
        "protocol=https\nhost=github.com\nwwwauth[]=Basic realm=x\nsomething_new=1\n\n")
    assert fields["protocol"] == "https" and fields["host"] == "github.com"
    assert fields["something_new"] == "1"
