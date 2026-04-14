"""
Tests for functions not covered by test_vulcan.py:
  - get_graph_path()
  - export()
  - import_data() — valid data, failed transact, malformed/unsafe facts
  - import-time path behavior
  - report_issue — gh available, gh unavailable, invalid issue type
  - retract() — reason required, success, returns tx count
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def temp_graph():
    """Create a temporary graph file for testing."""
    fd, path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


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


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vulcan
from vulcan import get_graph_path, export, import_data
from report_issue import report_issue


# ---------------------------------------------------------------------------
# get_graph_path()
# ---------------------------------------------------------------------------

def test_get_graph_path_returns_string():
    """Test that get_graph_path returns a non-empty string."""
    path = get_graph_path()
    assert isinstance(path, str)
    assert len(path) > 0


def test_get_graph_path_always_returns_cwd(tmp_path, monkeypatch):
    """Test that get_graph_path returns CWD memory.graph."""
    monkeypatch.chdir(tmp_path)
    result = vulcan.get_graph_path()
    assert result == str(tmp_path / "memory.graph")


def test_project_root_detection(tmp_path, monkeypatch):
    """Test that project root is simply CWD."""
    monkeypatch.chdir(tmp_path)
    result = vulcan._get_project_root_path()
    assert result == str(tmp_path / "memory.graph")


# ---------------------------------------------------------------------------
# export()
# ---------------------------------------------------------------------------

def test_export_missing_graph(tmp_path):
    """Test export returns error when graph doesn't exist."""
    path = str(tmp_path / "nonexistent.graph")
    result = export(graph_path=path)
    assert not result["ok"]
    assert "No graph file" in result["error"]


def test_export_returns_expected_shape(mock_minigraf, temp_graph):
    """Test export returns expected data structure."""
    with open(temp_graph, "w", encoding="utf-8") as f:
        f.write("")
    mock_minigraf.return_value = MagicMock(
        returncode=0,
        stdout="?e | ?a | ?v\n---\n:ent | :attr | \"val\"\n",
        stderr=""
    )
    result = export(graph_path=temp_graph)
    assert result["ok"]
    data = result["data"]
    assert "version" in data
    assert "exported_at" in data
    assert "facts" in data
    assert isinstance(data["facts"], list)


# ---------------------------------------------------------------------------
# import_data()
# ---------------------------------------------------------------------------

def test_import_data_empty_facts():
    """Test import_data returns error for empty facts list."""
    result = import_data({"facts": []})
    assert not result["ok"]
    assert "No facts to import" in result["error"]


def test_import_data_missing_facts_key():
    """Test import_data returns error when facts key is missing."""
    result = import_data({})
    assert not result["ok"]


def test_import_data_valid(mock_minigraf, temp_graph):
    """Test import_data with valid facts succeeds."""
    mock_minigraf.return_value = MagicMock(
        returncode=0,
        stdout="Transacted successfully (tx: 1)",
        stderr=""
    )
    data = {"facts": [[":e", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 1
    assert result["failed"] == 0


def test_import_data_failed_transact(mock_minigraf, temp_graph):
    """Test import_data counts failed transacts."""
    mock_minigraf.side_effect = subprocess.CalledProcessError(1, ["minigraf"], "", "some error")
    data = {"facts": [[":e", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


def test_import_data_malformed_fact(temp_graph):
    """Test import_data skips malformed facts."""
    data = {"facts": [[":e", ":attr"]]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


def test_import_data_unsafe_token(temp_graph):
    """Test import_data rejects unsafe tokens."""
    data = {"facts": [[":e]] [[:injected :x :y", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# report_issue
# ---------------------------------------------------------------------------

def test_report_issue_invalid_type():
    """Test report_issue rejects invalid issue types."""
    result = report_issue("not_a_valid_type", "some description")
    assert not result["ok"]
    assert "Invalid issue_type" in result["error"]


def test_report_issue_gh_unavailable():
    """Test report_issue falls back to logging when gh unavailable."""
    with patch("report_issue._check_gh_available", return_value=False):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "log"


def test_report_issue_no_repo(tmp_path):
    """Test report_issue falls back to logging when not in a repo."""
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue._get_current_repo", return_value=None), \
         patch("report_issue._is_minigraf_related", return_value=False):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "log"


def test_report_issue_gh_success():
    """Test report_issue creates issue via gh CLI."""
    mock_run = MagicMock()
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="https://github.com/owner/repo/issues/1",
        stderr=""
    )
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue._get_current_repo", return_value={"owner": "owner", "name": "repo"}), \
         patch("report_issue.subprocess.run", mock_run):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "gh"
    parsed = urlparse(result["result"])
    assert parsed.netloc in ("github.com", "github.local")


def test_report_issue_minigraf_bug_routes_to_minigraf_repo():
    """Test minigraf_bug routes to minigraf repo."""
    mock_run = MagicMock()
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="https://github.com/adityamukho/minigraf/issues/1",
        stderr=""
    )
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue.subprocess.run", mock_run):
        result = report_issue("minigraf_bug", "core engine bug")
    assert result["ok"]
    assert result["repo"] == "adityamukho/minigraf"


# ---------------------------------------------------------------------------
# retract()
# ---------------------------------------------------------------------------

def test_retract_requires_reason(mock_minigraf, temp_graph):
    """Test retract requires reason parameter."""
    from vulcan import retract
    result = retract("[[:test :attr \"value\"]]", reason=None, graph_path=temp_graph)
    assert not result["ok"]
    assert "reason is required" in result["error"]


def test_retract_reason_empty_fails(mock_minigraf, temp_graph):
    """Test retract fails with empty reason."""
    from vulcan import retract
    result = retract("[[:test :attr \"value\"]]", reason="", graph_path=temp_graph)
    assert not result["ok"]


def test_retract_success(mock_minigraf, temp_graph):
    """Test retract succeeds."""
    from vulcan import retract
    mock_minigraf.return_value = MagicMock(returncode=0, stdout="Retracted successfully (tx: 1)", stderr="")
    result = retract("[[:test :person/name \"Alice\"]]", reason="No longer needed", graph_path=temp_graph)
    assert result["ok"]
    assert result["tx"] == "1"


def test_retract_returns_tx_count(mock_minigraf, temp_graph):
    """Test retract returns transaction count."""
    from vulcan import retract
    mock_minigraf.return_value = MagicMock(returncode=0, stdout="Retracted successfully (tx: 42)", stderr="")
    result = retract("[[:old :attr \"value\"]]", reason="Obsolete", graph_path=temp_graph)
    assert result["ok"]
    assert result["tx"] == "42"