import json

from evals.at_scale.report import append_ingestion_report, append_query_report, write_json_result

SAMPLE_METRICS = {
    "repo_path": "/tmp/repo", "branch": "HEAD", "commits_ingested": 2,
    "wall_clock_seconds": 1.234, "throughput_per_minute": 97.2,
    "peak_rss_kb": 45000, "graph_size_bytes": 8192, "index_size_bytes": 4096,
    "status_latency": {"min": 0.001, "p50": 0.002, "p99": 0.004, "max": 0.005},
    "query_latency": {"min": 0.002, "p50": 0.003, "p99": 0.006, "max": 0.008},
    "final_status": "complete",
    "poll_count": 5, "poll_duty_fraction": 0.042,
    "checkpoint_summary": {
        "checkpoints": 52, "suppressed": 480, "total_seconds": 6.8,
        "elapsed_seconds": 141.5, "realised_duty": 0.0481,
    },
}


class TestWriteJsonResult:
    def test_writes_valid_json_file(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path)
        assert path.exists()
        assert json.loads(path.read_text()) == SAMPLE_METRICS

    def test_filename_has_prefix_and_timestamp(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path, prefix="ingestion")
        assert path.name.startswith("ingestion-")
        assert path.suffix == ".json"


class TestAppendIngestionReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert text.startswith("# At-Scale Code-Graph Benchmark")

    def test_appends_metrics_table_with_real_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "## Ingestion Run" in text
        assert "2" in text  # commits_ingested
        assert "complete" in text

    def test_second_call_appends_not_overwrites(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        first_len = len(report_path.read_text())
        append_ingestion_report(SAMPLE_METRICS, report_path)
        assert len(report_path.read_text()) > first_len
        assert report_path.read_text().count("## Ingestion Run") == 2

    def test_poll_duty_cycle_row_renders_with_formatted_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "Poll duty cycle (#242)" in text
        assert "4.20%" in text  # poll_duty_fraction=0.042 -> 4.20%
        assert "over 5 polls" in text  # poll_count=5

    def test_poll_duty_cycle_row_still_renders_for_a_pre_fix_result(self, tmp_path):
        # Every result JSON written before 2026-08-07 lacks the #242 poll
        # keys. Re-rendering one used to raise KeyError; the row must instead
        # render and say so, matching benchmark.md's own "treat its absence as
        # unmeasured, assume inflated" framing.
        pre_fix = {
            k: v for k, v in SAMPLE_METRICS.items()
            if k not in ("poll_count", "poll_duty_fraction")
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(pre_fix, report_path)
        text = report_path.read_text()
        assert "Poll duty cycle (#242)" in text
        assert "not measured" in text

    def test_checkpoint_duty_cycle_row_renders_with_formatted_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "4.81%" in text  # realised_duty=0.0481 -> 4.81%
        assert "52 checkpoints" in text
        assert "6.80s" in text  # total_seconds=6.8

    def test_checkpoint_duty_cycle_row_still_renders_for_a_pre_fix_result(self, tmp_path):
        # Mirrors the poll-duty precedent immediately above: a result JSON
        # written before #241 landed has no "checkpoint_summary" key at all.
        # Re-rendering one must not raise, and must say the row is
        # unmeasured rather than silently omitting it (#241 Task 6).
        pre_fix = {
            k: v for k, v in SAMPLE_METRICS.items() if k != "checkpoint_summary"
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(pre_fix, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "not measured" in text

    # --- #256 stderr-capture rows -------------------------------------
    #
    # benchmark.md is the DURABLE HUMAN RECORD, and it is the artifact the
    # prior #255 acceptance claim ("no errors") actually rested on. Before
    # these rows, a run that dropped commits, or one whose capture truncated,
    # was rendered identically to a clean one: "Final status | complete" and
    # nothing else. The JSON and the exit code were honest; this file was not.

    def _clean_256_metrics(self):
        return {
            **SAMPLE_METRICS,
            "stderr_capture_complete": True,
            "skipped_commits": [],
            "error_signals": [],
            "correction_sweep_summaries": [],
            "correction_sweep_skipped": 0,
        }

    def test_a_clean_run_reports_the_tee_active_and_the_capture_complete(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(self._clean_256_metrics(), report_path)
        text = report_path.read_text()
        assert "| Stderr tee (#256) | active" in text
        assert "| Stderr capture (#256) | complete |" in text
        assert "| Commits dropped (#256) | 0 |" in text
        assert "| Error signatures (#251/#256) | 0 |" in text

    def test_dropped_commits_are_visible_in_the_human_record(self, tmp_path):
        """The whole point: "Final status | complete" and "Commits ingested |
        2" are both blind to a dropped commit, so without this row the
        markdown cannot distinguish this run from a clean one."""
        metrics = {**self._clean_256_metrics(), "skipped_commits": ["deadbee1", "cafe002"]}
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Commits dropped (#256) | **2**" in text
        assert "deadbee1" in text and "cafe002" in text

    def test_error_signatures_are_visible_in_the_human_record(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "error_signals": [
                {"pattern": "page_out_of_bounds", "line": "Page 130 out of bounds"}
            ],
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Error signatures (#251/#256) | **1**: page_out_of_bounds |" in text

    def test_a_truncated_capture_is_flagged_and_its_counts_called_lower_bounds(self, tmp_path):
        """The dangerous case. skipped_commits/error_signals are EMPTY here --
        byte-identical to a clean run -- because the tee stopped collecting.
        The markdown must say so, or an empty count reads as proof of health."""
        metrics = {
            **self._clean_256_metrics(),
            "stderr_capture_complete": False,
            "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "INCOMPLETE" in text
        assert "LOWER BOUNDS" in text
        assert "pump did not complete cleanly" in text

    def test_the_256_rows_still_render_for_a_pre_fix_result(self, tmp_path):
        """Same defensive precedent as the poll and checkpoint rows: a result
        JSON written before 2026-08-16 carries none of these keys. Absence
        must render as "not measured", never as zero -- nothing was captured,
        so nothing is known."""
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)  # no #256 keys
        text = report_path.read_text()
        assert "| Stderr tee (#256) | none" in text
        assert "| Stderr capture (#256) | not measured" in text
        assert "| Commits dropped (#256) | not measured" in text
        assert "| Error signatures (#251/#256) | not measured" in text
        assert "NOT zero" in text


SAMPLE_QUERY_RESULTS = [
    {
        "id": 1, "category": "point-in-time", "passed": True,
        "actual": [[8]], "expected": [[8]],
        "minigraf_latency_seconds": 0.003, "baseline_latency_seconds": 0.015,
    },
    {
        "id": 2, "category": "delta", "passed": None,
        "actual": None, "expected": None,
        "minigraf_latency_seconds": 0.0, "baseline_latency_seconds": 0.0,
    },
]


class TestAppendQueryReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_RESULTS, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_reports_pass_fail_and_skipped(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_RESULTS, report_path)
        text = report_path.read_text()
        assert "## Query Correctness Run" in text
        assert "PASS" in text
        assert "SKIPPED (manual diff)" in text
