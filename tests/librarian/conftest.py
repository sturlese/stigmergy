import pytest

from stigmergy.librarian import githubapp


@pytest.fixture(autouse=True)
def no_ambient_github_app(monkeypatch):
    for name in (
        githubapp.APP_ID_ENV,
        githubapp.INSTALLATION_ID_ENV,
        githubapp.PRIVATE_KEY_ENV,
        githubapp.PRIVATE_KEY_FILE_ENV,
        githubapp.APP_LOGIN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
