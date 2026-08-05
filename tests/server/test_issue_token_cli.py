"""`stigmergy-issue-token` — the operator CLI: prints a fresh plaintext bearer token ONCE plus its
sha256 store line, and validates the email looks like one that could be a key in
`ops/identities.json`, which is keyed by email. Pure argparse/print plumbing over the
already-covered `issue()`; no DB, no network."""
from stigmergy.server.identity import hash_token
from stigmergy.server.issue_token import main


def test_non_email_argument_is_rejected_with_an_actionable_message(capsys):
    rc = main(["not-an-email"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not look like an email" in err
    assert "ops/identities.json" in err


def test_valid_email_prints_the_plaintext_once_and_the_store_line(capsys):
    rc = main(["ana@example.com"])
    assert rc == 0
    out = capsys.readouterr()

    lines = [line for line in out.out.splitlines() if line]
    assert len(lines) == 4   # comment / token / comment / store-line — nothing else on stdout
    token = lines[1]
    assert token and " " not in token   # a single opaque token, not a sentence

    # the sha256 store line lands on stdout too (the operator copy-pastes it into the store)
    assert f'"{hash_token(token)}": "ana@example.com"' in out.out
    # the "never stored" reminder is stderr — never mixed into the copy-pasteable stdout lines
    assert "never stored by this command" in out.err


def test_two_calls_never_repeat_a_token(capsys):
    main(["a@example.com"])
    first = [ln for ln in capsys.readouterr().out.splitlines() if ln][1]
    main(["b@example.com"])
    second = [ln for ln in capsys.readouterr().out.splitlines() if ln][1]
    assert first != second
