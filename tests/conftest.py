from pathlib import Path

import pytest

from aiagent.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(workspace=tmp_path, web_tools=False)
