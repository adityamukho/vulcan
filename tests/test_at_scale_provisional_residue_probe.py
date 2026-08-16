"""Unit tests for the #256 provisional-residue probe's analysis primitives.

M and N themselves are measurements of this repository's history, not
invariants, so they are not asserted. What IS asserted is that the
comparison fires when it should and that the inputs cannot be silently
misread -- the failure class the spec calls out.
"""

import pytest

from evals.at_scale.probe_provisional_residue import (
    breakdown_by_entity_type,
    provisional_entity_idents,
    read_sweep_total,
    require_complete_run,
    residue_verdict,
)


class TestReadSweepTotal:
    def test_reads_the_recorded_total(self):
        assert read_sweep_total({"correction_sweep_skipped": 17}) == 17

    def test_zero_is_a_valid_measured_value(self):
        """No summary line means zero, not unmeasured --
        _correction_sweep_log_summary prints only `if skipped_events:`."""
        assert read_sweep_total({"correction_sweep_skipped": 0}) == 0

    def test_a_missing_key_fails_loudly(self):
        """Defaulting to 0 would silently turn `M <= N` into `M == 0` and
        produce a false failure against a healthy graph."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"final_status": "complete"})

    def test_none_value_fails_with_diagnostic(self):
        """A metrics writer emitting null instead of omitting the field."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"correction_sweep_skipped": None})

    def test_non_numeric_string_fails_with_diagnostic(self):
        """Malformed value in the metrics file."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"correction_sweep_skipped": "not a number"})


class TestRequireCompleteRun:
    def test_accepts_a_complete_run(self):
        require_complete_run({"final_status": "complete"})

    def test_rejects_an_errored_run(self):
        """Residue on an aborted run means nothing."""
        with pytest.raises(SystemExit, match="complete"):
            require_complete_run({"final_status": "error"})

    def test_rejects_a_stopped_run(self):
        with pytest.raises(SystemExit, match="complete"):
            require_complete_run({"final_status": "stopped"})


class TestBreakdownByEntityType:
    def test_groups_by_the_ident_namespace(self):
        idents = [":function/a", ":function/b", ":module/c"]
        assert breakdown_by_entity_type(idents) == {"function": 2, "module": 1}

    def test_breakdown_sums_to_the_total(self):
        idents = [":function/a", ":class/b", ":module/c", ":function/d"]
        assert sum(breakdown_by_entity_type(idents).values()) == len(idents)

    def test_empty_input_yields_an_empty_breakdown(self):
        assert breakdown_by_entity_type([]) == {}

    def test_ident_with_no_slash_buckets_under_full_string(self):
        """Edge case: an ident without a slash."""
        idents = ["identifier_no_slash"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"identifier_no_slash": 1}

    def test_ident_with_no_leading_colon(self):
        """Edge case: an ident without leading colon."""
        idents = ["function/foo"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"function": 1}

    def test_empty_string_ident(self):
        """Edge case: an empty string ident."""
        idents = [""]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"": 1}

    def test_nested_namespace_ident(self):
        """Edge case: a nested namespace with multiple slashes."""
        idents = [":deeply/nested/type/name"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"deeply": 1}


class TestResidueVerdict:
    def test_m_below_n_passes(self):
        assert residue_verdict(3, 10)["ok"] is True

    def test_m_equal_to_n_passes(self):
        assert residue_verdict(10, 10)["ok"] is True

    def test_m_above_n_fails(self):
        """Provisional state the sweep never accounted for -- the #251
        signature this probe exists to detect."""
        assert residue_verdict(11, 10)["ok"] is False

    def test_both_raw_numbers_survive_in_the_verdict(self):
        """`M <= N` weakens as N grows, so a future run must be able to
        compare N itself across runs."""
        verdict = residue_verdict(3, 10)
        assert verdict["provisional_entities"] == 3
        assert verdict["sweep_skipped"] == 10


class TestProvisionalEntityIdents:
    """M is counted from a graph with a KNOWN number of planted markers, so a
    query that silently counts the wrong thing is visible."""

    def test_counts_exactly_the_planted_markers(self, tmp_path):
        import mcp_server

        graph = str(tmp_path / "residue.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                for ident in (":function/alpha", ":function/beta", ":module/gamma"):
                    mcp_server._lineage_mark_provisional(
                        db, ident, "2026-08-16T00:00:00Z"
                    )
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert sorted(found) == [":function/alpha", ":function/beta", ":module/gamma"]

    def test_a_confirmed_entity_leaves_no_residue(self, tmp_path):
        """_lineage_confirm retracts the marker, so a confirmed entity must
        stop counting toward M."""
        import mcp_server

        graph = str(tmp_path / "confirmed.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                mcp_server._lineage_mark_provisional(
                    db, ":function/alpha", "2026-08-16T00:00:00Z"
                )
                mcp_server._lineage_mark_provisional(
                    db, ":function/beta", "2026-08-16T00:00:00Z"
                )
                mcp_server._lineage_confirm(db, ":function/alpha")
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert found == [":function/beta"]

    def test_an_empty_graph_yields_no_residue(self, tmp_path):
        import mcp_server

        graph = str(tmp_path / "empty.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                assert provisional_entity_idents(db) == []
        finally:
            mcp_server._reset_db_state()

    def test_an_unmarked_entity_with_ordinary_facts_is_not_counted(self, tmp_path):
        """A query that accidentally matched on entity presence rather than
        marker presence would sweep this in too. Neither the confirmed-entity
        test nor the empty-graph test can catch that: this needs an entity
        that was simply never marked, sitting right next to one that was."""
        import mcp_server

        graph = str(tmp_path / "mixed.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                mcp_server._transact(
                    db,
                    '[[:module/untouched :entity-type :type/module] '
                    '[:module/untouched :ident ":module/untouched"] '
                    '[:module/untouched :description "untouched.py"]]',
                    "2026-08-16T00:00:00Z",
                )
                mcp_server._lineage_mark_provisional(
                    db, ":function/alpha", "2026-08-16T00:00:00Z"
                )
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert found == [":function/alpha"]
