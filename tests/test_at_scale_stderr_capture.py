"""Unit tests for the #256 stderr instrumentation.

The scanner is where this verification can fail OPEN: a scanner with a broken
pattern reports "no errors found" having matched nothing, which is
indistinguishable from a healthy run. So every pattern carries a positive
control, not just the clean-log negative control.

Fixtures are plain string literals on purpose. Building them through tmp_path
would bake each test's own name into the text, letting the scanner match the
path instead of the planted line.
"""

from evals.at_scale.stderr_capture import scan_ingestion_stderr

CLEAN = (
    "[_run_ingestion] starting\n"
    "[_run_ingestion] 705 commits linearized\n"
    "[_run_ingestion] done\n"
)


class TestSkippedCommits:
    def test_detects_a_write_failure_skip(self):
        text = (
            "[_run_ingestion] skipping commit abc123 ('subject'): "
            "write failed: boom\n"
        )
        assert scan_ingestion_stderr(text)["skipped_commits"] == ["abc123"]

    def test_detects_an_unreadable_commit_skip(self):
        text = "[_run_ingestion] skipping unreadable commit def456 ('s'): bad ref\n"
        assert scan_ingestion_stderr(text)["skipped_commits"] == ["def456"]

    def test_clean_log_yields_no_skips(self):
        assert scan_ingestion_stderr(CLEAN)["skipped_commits"] == []


class TestErrorSignals:
    """One positive control per #251 pattern. The page error is the reason
    these are regexes and not literals -- it carries live numbers."""

    def test_detects_page_out_of_bounds_with_real_numbers(self):
        text = "Page 130 out of bounds (total pages: 113)\n"
        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["page_out_of_bounds"]

    def test_detects_serde_deserialization_error(self):
        text = "Serde Deserialization Error: invalid tag\n"
        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["serde_deserialization_error"]

    def test_detects_expected_leaf_page(self):
        text = "stream_all_entries: expected leaf page, got branch\n"
        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["stream_all_entries_expected_leaf_page"]

    def test_signal_carries_the_matching_line(self):
        text = "Page 7 out of bounds (total pages: 3)\n"
        assert scan_ingestion_stderr(text)["error_signals"][0]["line"] == (
            "Page 7 out of bounds (total pages: 3)"
        )

    def test_clean_log_yields_no_signals(self):
        assert scan_ingestion_stderr(CLEAN)["error_signals"] == []


class TestCorrectionSweepTotal:
    def test_reads_the_summary_line(self):
        text = (
            "[_correction_sweep] 42 entities left provisional/unreconciled "
            "this run\n"
        )
        assert scan_ingestion_stderr(text)["correction_sweep_skipped"] == 42

    def test_absent_summary_means_zero_not_unmeasured(self):
        """_correction_sweep_log_summary only prints `if skipped_events:`
        (mcp_server.py:10598), so no line IS the zero case. The key must
        always be present -- the probe distinguishes a missing key (fail
        loudly) from a zero value (valid)."""
        scanned = scan_ingestion_stderr(CLEAN)
        assert scanned["correction_sweep_skipped"] == 0
        assert "correction_sweep_skipped" in scanned

    def test_multiple_summaries_are_all_recorded_and_max_is_used(self):
        """Two loops call _correction_sweep_log_summary, so one run can emit
        more than one line. The max is the smallest defensible N, which makes
        `M <= N` strictest -- a false alarm is investigable, a missed one is
        not."""
        text = (
            "[_correction_sweep] 5 entities left provisional/unreconciled this run\n"
            "[_correction_sweep] 9 entities left provisional/unreconciled this run\n"
        )
        scanned = scan_ingestion_stderr(text)
        assert scanned["correction_sweep_summaries"] == [5, 9]
        assert scanned["correction_sweep_skipped"] == 9
