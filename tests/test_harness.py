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
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minigraf_tool import query, transact, reset


class TestHarness:
    def __init__(self, graph_path=None):
        self.graph_path = graph_path or tempfile.mktemp(suffix=".graph")
    
    def setup(self):
        """Create test facts representing a coding session."""
        reset(self.graph_path)
        
        transact(
            "[[:project/cache :project/name \"distributed-cache\"] "
            "[:project/cache :project/priority \"low-latency\"] "
            "[:project/cache :decision/description \"use Redis\"]]",
            reason="Initial architecture decision",
            graph_path=self.graph_path
        )
        
        transact(
            "[[:component/auth :component/name \"AuthService\"] "
            "[:component/auth :calls :component/jwt]]",
            reason="Component dependency",
            graph_path=self.graph_path
        )
        
        return self
    
    def test_recall_accuracy(self):
        """Test: Can we retrieve stored decisions?"""
        result = query(
            "[:find ?priority :where [?e :project/priority ?priority]]",
            graph_path=self.graph_path
        )
        
        assert result["ok"], f"Query failed: {result.get('error')}"
        assert len(result["results"]) > 0, "No results returned"
        assert any("low-latency" in str(r) for r in result["results"]), \
            f"Expected 'low-latency', got: {result['results']}"
        
        print("✓ Recall accuracy: PASS")
        return True
    
    def test_dependency_query(self):
        """Test: Can we find what components exist?"""
        result = query(
            "[:find ?name :where [?e :component/name ?name]]",
            graph_path=self.graph_path
        )
        
        assert result["ok"], f"Query failed: {result.get('error')}"
        assert any("AuthService" in str(r) for r in result["results"]), \
            f"Expected 'AuthService', got: {result['results']}"
        
        print("✓ Dependency query: PASS")
        return True
    
    def test_temporal_query(self):
        """Test: Can we query at a specific transaction time?"""
        tx_before = 1
        
        result = query(
            "[:find ?desc :where [?e :decision/description ?desc]]",
            as_of=tx_before,
            graph_path=self.graph_path
        )
        
        assert result["ok"], f"Temporal query failed: {result.get('error')}"
        
        print("✓ Temporal query: PASS")
        return True
    
    def teardown(self):
        """Clean up test graph."""
        if os.path.exists(self.graph_path):
            os.remove(self.graph_path)


def run_tests():
    """Run all tests."""
    print("Running temporal-reasoning test harness...\n")
    
    harness = TestHarness()
    harness.setup()
    
    try:
        harness.test_recall_accuracy()
        harness.test_dependency_query()
        harness.test_temporal_query()
        
        print("\n✓ All tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    finally:
        harness.teardown()


if __name__ == "__main__":
    sys.exit(run_tests())
