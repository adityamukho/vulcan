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
