import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def temp_graph():
    """Create a temporary graph file for testing."""
    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)
    yield graph_path
    if os.path.exists(graph_path):
        os.remove(graph_path)


@pytest.fixture
def mock_minigraf():
    """Patch subprocess.run so tests run without a live minigraf binary."""
    with patch("vulcan.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Transacted successfully (tx: 1)",
            stderr=""
        )
        yield mock_run