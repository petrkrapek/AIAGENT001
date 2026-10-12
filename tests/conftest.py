import pytest

from aiagent.config import Config
from aiagent.tools import build_registry
from memory_repos import memory_repos

ENV_PREFIXES = ("AIAGENT_", "SMTP_", "AIRTABLE_", "ANTHROPIC_")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Tests must not see the developer's environment or any .env file."""
    import os

    for key in list(os.environ):
        if key.startswith(ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config() -> Config:
    return Config(web_tools=False, airtable_token="pat123", airtable_base_id="appTEST", send_pause=(0, 0))


@pytest.fixture
def repos():
    return memory_repos()


@pytest.fixture
def registry(config, repos):
    return build_registry(config, repos)


def call(registry, tool, **kwargs):
    return registry.get(tool).handler(**kwargs)


def rid(n: int) -> str:
    """Record id as the in-memory repositories (and Airtable) produce it."""
    return f"rec{n:014d}"
