"""Unit tests for the #256 provisional-residue probe's analysis primitives.

M and N themselves are measurements of this repository's history, not
invariants, so they are not asserted. What IS asserted is that the
comparison fires when it should and that the inputs cannot be silently
misread -- the failure class the spec calls out.
"""

import pytest

from evals.at_scale.probe_provisional_residue import (
    breakdown_by_entity_type,
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
