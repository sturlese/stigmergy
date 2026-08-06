"""No test run may touch state a human is using.

The Makefile `-include`s the operator's gitignored `.env` and `export`s it into every target, so
a variable that reaches `os.environ` reaches every test in the tree. `tests/conftest.py::
no_real_github_app_anywhere` clears the App's group for exactly that reason, and this is the file
that watches it.

**It lives at the ROOT on purpose, and that is the whole design of it.** The same assertions inside
`tests/librarian/` prove nothing about the root fixture: `tests/librarian/conftest.py` clears the
same group for its own package, so a variable dropped from the root tuple still looks cleared
there. Verified by mutation — with `APP_LOGIN_ENV` removed from the root tuple, the copy of this
test that lived beside the App's own unit tests stayed green. A guard that cannot fail is not a
guard, so it moved here, where only the root fixture applies.
"""
import os

import pytest

from stigmergy.librarian import githubapp

# Every variable the App reads. Kept as a literal rather than imported from `tests/conftest.py`'s
# own tuple: importing it would make this test assert that the tuple equals itself.
APP_ENV = (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
           githubapp.PRIVATE_KEY_FILE_ENV, githubapp.APP_LOGIN_ENV)


@pytest.mark.parametrize("name", APP_ENV)
def test_no_app_variable_from_the_operators_env_reaches_a_test(name):
    """Parametrized per variable so a failure NAMES the one that leaked.

    `APP_LOGIN_ENV` is the one this caught. It is not a credential, so it looked harmless and was
    left out of the group — but it decides the identity commits are AUTHORED by, and it is the
    variable an operator whose App is not named the default has to set. The moment a real
    deployment did, five tests asserting the bot's commit identity failed on a machine where
    nothing was wrong.
    """
    assert name not in os.environ, (
        f"{name} reached the test environment — add it to `_LIBRARIAN_APP_ENV` in "
        f"tests/conftest.py (and its twin in tests/librarian/conftest.py). Until then every test "
        f"asserting App behaviour passes or fails according to whoever ran it.")


def test_the_suite_sees_the_default_slug_however_the_deployment_is_named():
    """The property those five tests actually needed: with the environment cleared, `identity()`
    answers from the default, so an assertion about the bot's commit identity is about this
    software rather than about the machine it ran on."""
    assert githubapp.app_login() == githubapp.APP_LOGIN_DEFAULT
    assert githubapp.identity()[0] == githubapp.APP_LOGIN_DEFAULT
