"""
Tests for functions not covered by test_minigraf_tool.py:
  - get_graph_path()
  - export()
  - import_data() — valid data, failed transact, malformed/unsafe facts
  - HTTP mode (_run_http via MINIGRAF_MODE=http)
  - import-time path behavior
  - report_issue — gh available, gh unavailable, invalid issue type
"""

import importlib
import json
import os
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
    with patch("minigraf_tool.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Transacted successfully (tx: 1)",
            stderr=""
        )
        yield mock_run


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import minigraf_tool
from minigraf_tool import get_graph_path, export, import_data
from report_issue import report_issue


# ---------------------------------------------------------------------------
# get_graph_path()
# ---------------------------------------------------------------------------

def test_get_graph_path_returns_string():
    """Test that get_graph_path returns a non-empty string."""
    path = get_graph_path()
    assert isinstance(path, str)
    assert len(path) > 0


def test_get_graph_path_env_override(tmp_path):
    """Test that MINIGRAF_GRAPH_PATH env var overrides default."""
    custom = str(tmp_path / "custom.graph")
    with patch.dict(os.environ, {"MINIGRAF_GRAPH_PATH": custom}):
        result = minigraf_tool._get_default_graph_path()
    assert result == custom


def test_import_does_not_create_default_graph_dir(tmp_path):
    """Test that importing module doesn't create graph directory."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    module_name = "minigraf_tool"
    original = sys.modules.pop(module_name, None)

    try:
        with patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False), \
             patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir should not run during import")):
            imported = importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if original is not None:
            sys.modules[module_name] = original

    expected = fake_home / ".local" / "share" / "temporal-reasoning" / "memory.graph"
    assert imported.DEFAULT_GRAPH_PATH == str(expected)


def test_transact_creates_default_graph_parent_dir_lazily(tmp_path, mock_minigraf):
    """Test that transact creates parent directory on first write."""
    fake_graph = tmp_path / "nested" / "memory.graph"

    with patch.object(minigraf_tool, "DEFAULT_GRAPH_PATH", str(fake_graph)):
        result = minigraf_tool.transact(
            "[[:test :person/name \"Alice\"]]",
            reason="create graph on first write"
        )

    assert result["ok"]
    assert fake_graph.parent.exists()


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
# HTTP mode
# ---------------------------------------------------------------------------

def test_run_http_success():
    """Test _run_http handles successful response."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"results": [["val"]]}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("minigraf_tool.urllib.request.urlopen", return_value=mock_response):
        result = minigraf_tool._run_http("query", {"datalog": "[:find ?x :where [?e :a ?x]]"})

    assert result["ok"]
    assert "data" in result


def test_http_mode_query_calls_run_http(temp_graph):
    """Test query uses HTTP mode when configured."""
    with open(temp_graph, "w", encoding="utf-8") as f:
        f.write("")
    with patch.dict(os.environ, {"MINIGRAF_MODE": "http"}):
        with patch("minigraf_tool._run_http") as mock_http:
            mock_http.return_value = {"ok": True, "data": {"results": []}}
            saved = minigraf_tool.MINIGRAF_MODE
            minigraf_tool.MINIGRAF_MODE = "http"
            try:
                result = minigraf_tool.query(
                    "[:find ?x :where [?e :a ?x]]", graph_path=temp_graph
                )
            finally:
                minigraf_tool.MINIGRAF_MODE = saved
    mock_http.assert_called_once()
    assert result["ok"]


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