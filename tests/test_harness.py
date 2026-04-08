#!/usr/bin/env python3
"""
Test harness to validate temporal-reasoning skill is useful.

Tests:
1. Recall accuracy - does agent remember key facts?
2. Behavioral consistency - does agent act according to past decisions?
3. Prompt compression - do we need to repeat less context?
"""

import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minigraf_tool import query, transact, reset


@pytest.fixture
def populated_graph():
    """Create a temporary graph pre-populated with test facts."""
    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)

    reset(graph_path)
    transact(
        "[[:project/cache :project/name \"distributed-cache\"] "
        "[:project/cache :project/priority \"low-latency\"] "
        "[:project/cache :decision/description \"use Redis\"]]",
        reason="Initial architecture decision",
        graph_path=graph_path
    )
    transact(
        "[[:component/auth :component/name \"AuthService\"] "
        "[:component/auth :calls :component/jwt]]",
        reason="Component dependency",
        graph_path=graph_path
    )

    yield graph_path

    if os.path.exists(graph_path):
        os.remove(graph_path)


def test_recall_accuracy(populated_graph):
    """Test: Can we retrieve stored decisions?"""
    result = query(
        "[:find ?priority :where [?e :project/priority ?priority]]",
        graph_path=populated_graph
    )

    assert result["ok"], f"Query failed: {result.get('error')}"
    assert len(result["results"]) > 0, "No results returned"
    assert any("low-latency" in str(r) for r in result["results"]), \
        f"Expected 'low-latency', got: {result['results']}"


def test_dependency_query(populated_graph):
    """Test: Can we find what components exist?"""
    result = query(
        "[:find ?name :where [?e :component/name ?name]]",
        graph_path=populated_graph
    )

    assert result["ok"], f"Query failed: {result.get('error')}"
    assert any("AuthService" in str(r) for r in result["results"]), \
        f"Expected 'AuthService', got: {result['results']}"


def test_temporal_query(populated_graph):
    """Test: Can we query at a specific transaction time?"""
    result = query(
        "[:find ?desc :as-of 1 :where [?e :decision/description ?desc]]",
        graph_path=populated_graph
    )

    assert result["ok"], f"Temporal query failed: {result.get('error')}"


def test_reason_required():
    """Test: transact requires reason parameter."""
    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)
    try:
        result = transact(
            "[[:test :person/name \"Alice\"]]",
            reason=None,
            graph_path=graph_path
        )
        assert not result["ok"], "transact should fail without reason"
        assert "reason is required for all writes" in result.get("error", "")

        result_empty = transact(
            "[[:test :person/name \"Bob\"]]",
            reason="",
            graph_path=graph_path
        )
        assert not result_empty["ok"], "transact should fail with empty reason"
        assert "reason is required for all writes" in result_empty.get("error", "")
    finally:
        if os.path.exists(graph_path):
            os.remove(graph_path)


# Standalone runner for use without pytest
def run_tests():
    """Run all tests without pytest (for direct invocation)."""
    import sys
    print("Running temporal-reasoning test harness...\n")

    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)

    reset(graph_path)
    transact(
        "[[:project/cache :project/name \"distributed-cache\"] "
        "[:project/cache :project/priority \"low-latency\"] "
        "[:project/cache :decision/description \"use Redis\"]]",
        reason="Initial architecture decision",
        graph_path=graph_path
    )
    transact(
        "[[:component/auth :component/name \"AuthService\"] "
        "[:component/auth :calls :component/jwt]]",
        reason="Component dependency",
        graph_path=graph_path
    )

    try:
        test_recall_accuracy(graph_path)
        print("✓ Recall accuracy: PASS")
        test_dependency_query(graph_path)
        print("✓ Dependency query: PASS")
        test_temporal_query(graph_path)
        print("✓ Temporal query: PASS")
        test_reason_required()
        print("✓ Reason required: PASS")
        print("\n✓ All tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    finally:
        if os.path.exists(graph_path):
            os.remove(graph_path)


if __name__ == "__main__":
    sys.exit(run_tests())
