"""`stigmergy-admin-token` — the printed pair is real, fresh every run, and stored nowhere."""
import hashlib

from stigmergy.admin.cli import main
from stigmergy.admin.settings import TOKEN_HASH_ENV


def _issue(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line and not line.startswith("#")]
    token = lines[0]
    hash_line = next(line for line in lines if line.startswith(TOKEN_HASH_ENV + "="))
    return token, hash_line.split("=", 1)[1]


def test_the_printed_hash_is_the_sha256_of_the_printed_token(capsys):
    token, digest = _issue(capsys)
    assert hashlib.sha256(token.encode()).hexdigest() == digest


def test_two_runs_mint_two_different_tokens(capsys):
    first, _ = _issue(capsys)
    second, _ = _issue(capsys)
    assert first != second
