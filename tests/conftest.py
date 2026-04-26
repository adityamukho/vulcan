import pytest
from unittest.mock import MagicMock


@pytest.fixture
def temp_graph(tmp_path):
    """Return a path to a non-existent .graph file in a temp directory."""
    return str(tmp_path / "test.graph")


@pytest.fixture
def mock_db():
    """Mock MiniGrafDb instance — avoids needing a live minigraf install."""
    db = MagicMock()
    db.execute.return_value = '{"results": []}'
    return db
