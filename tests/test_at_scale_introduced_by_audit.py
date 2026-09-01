# tests/test_at_scale_introduced_by_audit.py
"""#287: detection of the two-value :introduced-by corruption from #235.

The unit under test is PURE -- it reads the fact Counter that
`fact_audit._graph_facts` already builds -- so these tests construct that
Counter directly rather than a graph. The real-backend half, where the two
values are transacted into an actual graph and the audit reports them, lives
in tests/test_at_scale_fact_audit.py; a pure function cannot prove that the
Counter it is handed has the shape it assumes.
"""

from collections import Counter

from evals.at_scale.introduced_by_audit import introduced_by_duplicates


def _facts(*triples):
    """A Counter shaped like fact_audit._graph_facts' output."""
    return Counter({t: 1 for t in triples})


class TestTheCorruptionIsDetected:
    def test_two_values_on_one_entity_is_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["entities"] == 1

    def test_a_single_value_is_not_reported(self):
        """The overwhelmingly common case. A detector that fired here would
        condemn every healthy graph."""
        result = introduced_by_duplicates(
            _facts(("uuid-a", ":introduced-by", ":commit/aaa"))
        )
        assert result["entities"] == 0
        assert result["sample"] == []

    def test_an_entity_with_no_introduced_by_is_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":description", "a function"),
                ("uuid-a", ":entity-type", ":type/function"),
            )
        )
        assert result["entities"] == 0

    def test_three_values_still_counts_as_one_entity(self):
        """The unit is the affected ENTITY, not the surplus fact: the answer
        the user needs is how many entities are wrong, and the standing
        decision is the same at two values as at three."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
                ("uuid-a", ":introduced-by", ":commit/ccc"),
            )
        )
        assert result["entities"] == 1
        assert result["sample"][0][1] == [":commit/aaa", ":commit/bbb", ":commit/ccc"]

    def test_two_affected_entities_count_separately(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
                ("uuid-b", ":introduced-by", ":commit/ccc"),
                ("uuid-b", ":introduced-by", ":commit/ddd"),
            )
        )
        assert result["entities"] == 2


class TestLegitimatelyMultiValuedAttributesAreNotSwept:
    """The one way this check could be catastrophically wrong.

    `:contains`, `:modified-in` and `:depends-on` hold many values per entity
    BY DESIGN -- a module contains every function in it. A duplicate detector
    that keyed on "entity has two values for an attribute" rather than on
    `:introduced-by` specifically would report every module in every healthy
    graph and condemn it.
    """

    def test_a_module_containing_many_functions_is_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-mod", ":contains", ":function/f1"),
                ("uuid-mod", ":contains", ":function/f2"),
                ("uuid-mod", ":contains", ":function/f3"),
            )
        )
        assert result["entities"] == 0

    def test_many_modified_in_edges_are_not_reported(self):
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":modified-in", ":commit/aaa"),
                ("uuid-a", ":modified-in", ":commit/bbb"),
            )
        )
        assert result["entities"] == 0

    def test_the_corruption_is_still_found_alongside_them(self):
        """The other half: narrowing to :introduced-by must not narrow so far
        that a real duplicate sitting next to legitimate multi-valued facts
        goes unseen."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":contains", ":function/f1"),
                ("uuid-a", ":contains", ":function/f2"),
                ("uuid-a", ":modified-in", ":commit/xxx"),
                ("uuid-a", ":modified-in", ":commit/yyy"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["entities"] == 1


class TestTheSampleNamesTheEntity:
    def test_the_sample_uses_the_entitys_ident_when_the_scan_carries_one(self):
        """A raw UUID is useless to someone deciding whether to rebuild. Every
        code entity carries a self-referencing `[ident :ident "ident"]` fact
        (_build_code_triples), and it is in the same Counter, so naming the
        entity costs nothing extra."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":ident", ":function/mcp_server.py::foo"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["sample"] == [
            [":function/mcp_server.py::foo", [":commit/aaa", ":commit/bbb"]]
        ]

    def test_an_entity_with_no_ident_fact_falls_back_to_the_raw_entity(self):
        """Not every entity that can hold :introduced-by is guaranteed to
        carry an :ident -- a partial write is exactly the state this audit
        exists to find, and dropping such an entity from the sample would hide
        the worst case."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/aaa"),
                ("uuid-a", ":introduced-by", ":commit/bbb"),
            )
        )
        assert result["sample"] == [["uuid-a", [":commit/aaa", ":commit/bbb"]]]
        assert result["entities"] == 1

    def test_the_values_are_sorted(self):
        """minigraf imposes no ordering on query results
        (_entity_introduced_by_values_query: "in the backend's unspecified
        order"), and a Counter's iteration order is insertion order. Sorting
        is what makes a recorded sample comparable across two runs."""
        result = introduced_by_duplicates(
            _facts(
                ("uuid-a", ":introduced-by", ":commit/zzz"),
                ("uuid-a", ":introduced-by", ":commit/aaa"),
            )
        )
        assert result["sample"][0][1] == [":commit/aaa", ":commit/zzz"]

    def test_the_sample_is_capped_but_the_count_is_not(self):
        """A corrupt at-scale graph can hold thousands of ambiguous entities
        (#235's own log cap exists for that reason). The COUNT is the number
        the decision rests on, so it must stay exact."""
        triples = []
        for i in range(20):
            triples.append((f"uuid-{i:02d}", ":introduced-by", ":commit/aaa"))
            triples.append((f"uuid-{i:02d}", ":introduced-by", ":commit/bbb"))
        result = introduced_by_duplicates(_facts(*triples), sample_cap=5)
        assert result["entities"] == 20
        assert len(result["sample"]) == 5


class TestRepeatsOfOneValue:
    def test_the_same_value_twice_is_not_the_two_value_corruption(self):
        """#235 is two DIFFERENT commits, and the Counter's key already
        collapses identical (entity, attribute, value) triples into one key
        with a count. Reading the count instead of the number of distinct
        values would report an entity whose only defect is a duplicated
        identical fact -- a different problem, with a different answer."""
        result = introduced_by_duplicates(
            Counter({("uuid-a", ":introduced-by", ":commit/aaa"): 2})
        )
        assert result["entities"] == 0
