import pytest

from app.config import RealtimeSettings
from app.storage import Repository


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "data.sqlite3")


@pytest.fixture
def settings():
    return RealtimeSettings(provider="doubao")
