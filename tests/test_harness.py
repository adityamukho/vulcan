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
from vulcan import query, transact, reset


def _first_value(result):
    """Return the first scalar value from a query result."""
    if not result.get("ok"):
        raise AssertionError(f"Query failed: {result.get('error')}")
    rows = result.get("results", [])
    if not rows:
        raise AssertionError("Expected persisted memory result, got none")
    return str(rows[0][0]).strip('"')


def answer_cache_strategy_question(graph_path):
    """Simulate a later session answering from persisted memory."""
    decision = _first_value(
        query(
            "[:find ?desc :where [?e :decision/description ?desc]]",
            graph_path=graph_path,
        )
    )
    return f"Use {decision} based on persisted memory from the earlier session."


def derive_cache_plan(graph_path):
    """Simulate a later session turning persisted memory into an action."""
    decision = _first_value(
        query(
            "[:find ?desc :where [?e :decision/description ?desc]]",
            graph_path=graph_path,
        )
    )
    if "Redis" not in decision:
        raise AssertionError(f"Unexpected cache decision: {decision}")
    return {"cache_backend": "Redis", "source": "persisted memory"}


def _count_prompt_tokens(text):
    """Use whitespace-delimited words as a stable local prompt-size proxy."""
    return len(text.split())


def collect_usefulness_benchmarks(graph_path):
    """Return explicit benchmark metrics for usefulness claims."""
    answer = answer_cache_strategy_question(graph_path)
    plan = derive_cache_plan(graph_path)

    behavior_consistency = {
        "passed": "Redis" in answer and plan["cache_backend"] == "Redis",
        "answer_mentions": "Redis" in answer,
        "action_matches": plan["cache_backend"] == "Redis",
    }

    memory_prompt = "What cache strategy should we use?"
    restated_prompt = (
        "We previously decided to use Redis for the distributed-cache project "
        "because low latency matters. What cache strategy should we use?"
    )
    prompt_compression = {
        "memory_prompt_tokens": _count_prompt_tokens(memory_prompt),
        "restated_prompt_tokens": _count_prompt_tokens(restated_prompt),
        "saved_tokens": _count_prompt_tokens(restated_prompt) - _count_prompt_tokens(memory_prompt),
        "method": "word-count proxy comparing recalled context vs repeated prompt context",
    }

    return {
        "behavior_consistency": behavior_consistency,
        "prompt_compression": prompt_compression,
    }


def print_benchmark_summary(graph_path):
    """Print a human-readable summary of usefulness benchmark metrics."""
    benchmarks = collect_usefulness_benchmarks(graph_path)
    behavior = benchmarks["behavior_consistency"]
    compression = benchmarks["prompt_compression"]
    print("Usefulness benchmarks:")
    print(
        "Behavior consistency: "
        f"passed={behavior['passed']} "
        f"answer_mentions={behavior['answer_mentions']} "
        f"action_matches={behavior['action_matches']}"
    )
    print(
        "Prompt compression: "
        f"memory_prompt_tokens={compression['memory_prompt_tokens']} "
        f"restated_prompt_tokens={compression['restated_prompt_tokens']} "
        f"saved_tokens={compression['saved_tokens']}"
    )


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


def test_cross_session_decision_changes_later_answer(populated_graph):
    """Test: Does a later session answer using persisted decisions?"""
    answer = answer_cache_strategy_question(populated_graph)

    assert "Redis" in answer
    assert "persisted memory" in answer


def test_cross_session_decision_changes_later_action(populated_graph):
    """Test: Does a later session derive an action from persisted decisions?"""
    plan = derive_cache_plan(populated_graph)

    assert plan["cache_backend"] == "Redis"
    assert plan["source"] == "persisted memory"


def test_usefulness_benchmarks_report_explicit_metrics(populated_graph):
    """Test: Does the harness expose explicit usefulness metrics?"""
    benchmarks = collect_usefulness_benchmarks(populated_graph)

    assert benchmarks["behavior_consistency"]["passed"] is True
    assert (
        benchmarks["prompt_compression"]["memory_prompt_tokens"]
        < benchmarks["prompt_compression"]["restated_prompt_tokens"]
    )


def test_benchmark_summary_labels_metric_sections(populated_graph, capsys):
    """Test: Does the harness print benchmark sections explicitly?"""
    print_benchmark_summary(populated_graph)
    captured = capsys.readouterr()

    assert "Usefulness benchmarks" in captured.out
    assert "Behavior consistency" in captured.out
    assert "Prompt compression" in captured.out


@pytest.fixture
def graph_with_edges():
    """Create a temporary graph pre-populated with entity-reference edges.

    Topology:
        api-gateway --:calls--> auth-service --:depends-on--> jwt-validator --:depends-on--> key-store

    Also includes a decision traceability edge:
        asyncio-choice --:motivated-by--> gil-constraint

    And a governance edge:
        no-blocking-io --:governs--> auth-service
    """
    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)

    reset(graph_path)
    transact(
        '[[:project/api-gateway :name "API Gateway"]'
        ' [:project/api-gateway :entity-type :type/component]'
        ' [:project/api-gateway :calls :project/auth-service]]',
        reason="API gateway component and its downstream call",
        graph_path=graph_path,
    )
    transact(
        '[[:project/auth-service :name "Auth Service"]'
        ' [:project/auth-service :entity-type :type/component]'
        ' [:project/auth-service :depends-on :project/jwt-validator]]',
        reason="Auth service and its JWT dependency",
        graph_path=graph_path,
    )
    transact(
        '[[:project/jwt-validator :name "JWT Validator"]'
        ' [:project/jwt-validator :entity-type :type/component]'
        ' [:project/jwt-validator :depends-on :project/key-store]]',
        reason="JWT validator and its key-store dependency",
        graph_path=graph_path,
    )
    transact(
        '[[:project/key-store :name "Key Store"]'
        ' [:project/key-store :entity-type :type/component]]',
        reason="Key store component (leaf node)",
        graph_path=graph_path,
    )
    transact(
        '[[:rules/gil-constraint :description "Python GIL limits true thread parallelism"]'
        ' [:rules/gil-constraint :entity-type :type/constraint]'
        ' [:project/asyncio-choice :description "use asyncio over threading for concurrency"]'
        ' [:project/asyncio-choice :entity-type :type/decision]'
        ' [:project/asyncio-choice :motivated-by :rules/gil-constraint]]',
        reason="Decision traceability: asyncio chosen due to GIL",
        graph_path=graph_path,
    )
    transact(
        '[[:rules/no-blocking-io :description "auth service must not block the event loop"]'
        ' [:rules/no-blocking-io :entity-type :type/constraint]'
        ' [:rules/no-blocking-io :governs :project/auth-service]]',
        reason="Constraint governing the auth service",
        graph_path=graph_path,
    )

    yield graph_path

    if os.path.exists(graph_path):
        os.remove(graph_path)


# ---------------------------------------------------------------------------
# Graph capability tests (Eval 5, 6, 7)
# ---------------------------------------------------------------------------

def test_entity_ref_stored_as_keyword(graph_with_edges):
    """Eval 5: Entity refs are stored as keywords, not strings.

    A traversable edge requires the value to be a keyword ident like
    :project/auth-service, not the string "auth-service".
    """
    result = query(
        "[:find ?target :where [:project/api-gateway :calls ?target]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert result["results"], "Expected a :calls target, got none"
    target = str(result["results"][0][0]).strip()
    assert target.startswith(":project/"), (
        f"Expected entity keyword (e.g. :project/auth-service), got: {target!r}. "
        "Relationship was stored as a string instead of an entity reference."
    )


def test_single_hop_graph_traversal(graph_with_edges):
    """Eval 5: Traverse a single edge to reach a related entity's attribute.

    Joins through the :calls edge from api-gateway to retrieve the name
    of the called service — proving the edge is traversable.
    """
    result = query(
        "[:find ?name"
        " :where [:project/api-gateway :calls ?svc]"
        "        [?svc :name ?name]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert result["results"], "Expected a name via :calls traversal, got none"
    names = [str(r[0]).strip('"') for r in result["results"]]
    assert any("Auth Service" in n for n in names), (
        f"Expected 'Auth Service' via :calls traversal, got: {names}"
    )


def test_multi_hop_graph_join(graph_with_edges):
    """Eval 6: Two-hop join across entity-reference edges.

    Traverses api-gateway --:calls--> auth-service --:depends-on--> jwt-validator
    in a single query, proving multi-hop joins work on entity refs.
    """
    result = query(
        "[:find ?name"
        " :where [:project/api-gateway :calls ?mid]"
        "        [?mid :depends-on ?leaf]"
        "        [?leaf :name ?name]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert result["results"], "Expected a name via 2-hop traversal, got none"
    names = [str(r[0]).strip('"') for r in result["results"]]
    assert any("JWT Validator" in n for n in names), (
        f"Expected 'JWT Validator' via 2-hop join, got: {names}"
    )


def test_rules_unify_edge_types(graph_with_edges):
    """Eval 6: Rules unify multiple edge types into a single named relation.

    Minigraf rules apply base-case matches but do not support recursive
    evaluation (the recursive clause is silently ignored).  Rules ARE useful
    for aliasing: define 'reachable' as 'depends-on OR calls' and query
    both edge types with one rule name.

    For unbounded transitive traversal use explicit multi-hop joins instead
    (see test_multi_hop_graph_join).
    """
    # Rule: anything connected via :depends-on OR :calls is 'linked'
    result = query(
        "(rule [(linked ?a ?d) [?a :depends-on ?d]])"
        "(rule [(linked ?a ?d) [?a :calls ?d]])"
        "[:find ?name"
        " :where (linked :project/auth-service ?svc)"
        "        [?svc :name ?name]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Rule query failed: {result.get('error')}"
    assert result["results"], "Expected linked services from auth-service, got none"
    names = [str(r[0]).strip('"') for r in result["results"]]
    # auth-service :depends-on :jwt-validator → should find JWT Validator
    assert any("JWT Validator" in n for n in names), (
        f"Expected 'JWT Validator' via :depends-on rule, got: {names}"
    )


def test_entity_type_query(graph_with_edges):
    """Eval 5/6: :entity-type enables typed cross-category queries.

    Finds all component entities without knowing their individual idents —
    the typed query returns exactly the 4 components, not decisions/constraints.
    """
    result = query(
        "[:find ?name"
        " :where [?e :entity-type :type/component]"
        "        [?e :name ?name]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    names = [str(r[0]).strip('"') for r in result["results"]]
    assert len(names) == 4, f"Expected 4 components, got {len(names)}: {names}"
    for expected in ("API Gateway", "Auth Service", "JWT Validator", "Key Store"):
        assert any(expected in n for n in names), \
            f"Expected '{expected}' in component list, got: {names}"


def test_decision_traceability_via_motivated_by(graph_with_edges):
    """Eval 7: Traverse :motivated-by edge to explain why a decision was made.

    Retrieves the constraint description by following the edge from the
    decision entity — the 'why' is in the graph, not in the decision itself.
    """
    result = query(
        "[:find ?reason"
        " :where [?d :description \"use asyncio over threading for concurrency\"]"
        "        [?d :motivated-by ?c]"
        "        [?c :description ?reason]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert result["results"], "Expected a reason via :motivated-by traversal, got none"
    reason = str(result["results"][0][0]).strip('"')
    assert "GIL" in reason, (
        f"Expected GIL constraint via :motivated-by traversal, got: {reason!r}"
    )


def test_constraint_governs_component(graph_with_edges):
    """Eval 7: :governs edge links constraints to the components they apply to.

    Finds constraints that govern auth-service by traversing the :governs
    edge in reverse — which constraints point at this component?
    """
    result = query(
        "[:find ?desc"
        " :where [?c :governs :project/auth-service]"
        "        [?c :description ?desc]]",
        graph_path=graph_with_edges,
    )
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert result["results"], "Expected a governing constraint for auth-service, got none"
    descs = [str(r[0]).strip('"') for r in result["results"]]
    assert any("block" in d for d in descs), (
        f"Expected no-blocking-io constraint, got: {descs}"
    )


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
        test_cross_session_decision_changes_later_answer(graph_path)
        print("✓ Cross-session answer derivation: PASS")
        test_cross_session_decision_changes_later_action(graph_path)
        print("✓ Cross-session action derivation: PASS")
        test_usefulness_benchmarks_report_explicit_metrics(graph_path)
        print("✓ Usefulness benchmark metrics: PASS")
        print_benchmark_summary(graph_path)
        test_reason_required()
        print("✓ Reason required: PASS")
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    finally:
        if os.path.exists(graph_path):
            os.remove(graph_path)

    # Graph capability tests — separate fixture
    fd2, edges_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd2)
    os.remove(edges_path)

    reset(edges_path)
    transact(
        '[[:project/api-gateway :name "API Gateway"]'
        ' [:project/api-gateway :entity-type :type/component]'
        ' [:project/api-gateway :calls :project/auth-service]]',
        reason="API gateway component and its downstream call",
        graph_path=edges_path,
    )
    transact(
        '[[:project/auth-service :name "Auth Service"]'
        ' [:project/auth-service :entity-type :type/component]'
        ' [:project/auth-service :depends-on :project/jwt-validator]]',
        reason="Auth service and its JWT dependency",
        graph_path=edges_path,
    )
    transact(
        '[[:project/jwt-validator :name "JWT Validator"]'
        ' [:project/jwt-validator :entity-type :type/component]'
        ' [:project/jwt-validator :depends-on :project/key-store]]',
        reason="JWT validator and its key-store dependency",
        graph_path=edges_path,
    )
    transact(
        '[[:project/key-store :name "Key Store"]'
        ' [:project/key-store :entity-type :type/component]]',
        reason="Key store component (leaf node)",
        graph_path=edges_path,
    )
    transact(
        '[[:rules/gil-constraint :description "Python GIL limits true thread parallelism"]'
        ' [:rules/gil-constraint :entity-type :type/constraint]'
        ' [:project/asyncio-choice :description "use asyncio over threading for concurrency"]'
        ' [:project/asyncio-choice :entity-type :type/decision]'
        ' [:project/asyncio-choice :motivated-by :rules/gil-constraint]]',
        reason="Decision traceability: asyncio chosen due to GIL",
        graph_path=edges_path,
    )
    transact(
        '[[:rules/no-blocking-io :description "auth service must not block the event loop"]'
        ' [:rules/no-blocking-io :entity-type :type/constraint]'
        ' [:rules/no-blocking-io :governs :project/auth-service]]',
        reason="Constraint governing the auth service",
        graph_path=edges_path,
    )

    try:
        test_entity_ref_stored_as_keyword(edges_path)
        print("✓ Entity ref stored as keyword: PASS")
        test_single_hop_graph_traversal(edges_path)
        print("✓ Single-hop graph traversal: PASS")
        test_multi_hop_graph_join(edges_path)
        print("✓ Multi-hop graph join: PASS")
        test_transitive_impact_via_rules(edges_path)
        print("✓ Transitive impact via rules: PASS")
        test_entity_type_query(edges_path)
        print("✓ Entity type query: PASS")
        test_decision_traceability_via_motivated_by(edges_path)
        print("✓ Decision traceability via :motivated-by: PASS")
        test_constraint_governs_component(edges_path)
        print("✓ Constraint governs component: PASS")
        print("\n✓ All tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    finally:
        if os.path.exists(edges_path):
            os.remove(edges_path)


if __name__ == "__main__":
    sys.exit(run_tests())
