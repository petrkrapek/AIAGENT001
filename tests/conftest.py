import pytest

from aiagent.config import Config
from aiagent.repos import memory_repos
from aiagent.tools import build_registry


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(web_tools=False, profile_path=tmp_path / "profil.md",
                  airtable_token="pat123", airtable_base_id="appTEST")


@pytest.fixture
def repos():
    return memory_repos()


@pytest.fixture
def registry(config, repos):
    return build_registry(config, repos)


def call(registry, tool, **kwargs):
    return registry.get(tool).handler(**kwargs)
