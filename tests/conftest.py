import pytest


@pytest.fixture(autouse=True)
def _no_github_mirror(monkeypatch):
    """The app starts the GitHub mirror loop on FileStore; never sync test data to the real archive."""
    monkeypatch.setenv("THREADVAULT_GH_REPO", "")
