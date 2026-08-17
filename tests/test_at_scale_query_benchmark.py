# tests/test_at_scale_query_benchmark.py
import json
import subprocess as _subprocess

import pytest

from evals.at_scale.run_query_benchmark import _exit_code, run_query_benchmark


CLEAN_INGESTION = {
    "final_status": "complete",
    "commits_ingested": 2,
    "skipped_commits": [],
    "error_signals": [],
    "stderr_capture_complete": True,
}


def _report(entries, **ingestion_overrides):
    return {
        "entries": entries,
        "ingestion": {**CLEAN_INGESTION, **ingestion_overrides},
    }


class TestExitCode:
    def test_zero_when_all_scored_entries_pass_and_ingestion_was_clean(self):
        entries = [
            {"id": 1, "passed": True},
            {"id": 2, "passed": None},
            {"id": 3, "passed": True},
        ]
        assert _exit_code(_report(entries)) == 0

    def test_nonzero_when_any_entry_fails(self):
        entries = [{"id": 1, "passed": True}, {"id": 2, "passed": False}]
        assert _exit_code(_report(entries)) == 1

    def test_zero_for_empty_entries(self):
        assert _exit_code(_report([])) == 0

    def test_an_absent_ingestion_key_evaluates_as_clean(self):
        # Matching run_ingestion_benchmark._exit_code's posture toward inputs
        # that predate an instrument: unknown is not failure.
        assert _exit_code({"entries": [{"id": 1, "passed": True}]}) == 0

    @pytest.mark.parametrize(
        "override",
        [
            {"skipped_commits": ["deadbee1"]},
            {"error_signals": [{"pattern": "page_out_of_bounds"}]},
            {"stderr_capture_complete": False},
            {"final_status": "error"},
            {"tee_failure": "TeeStderrFailure('pump did not complete cleanly')"},
        ],
        ids=["dropped-commit", "error-signature", "truncated-capture", "errored-run", "tee-failure"],
    )
    def test_each_unclean_ingestion_clause_fails_the_run_on_its_own(self, override):
        """#275: the fail-open. Every query entry passes here; the graph those
        latencies were measured over is the thing that is wrong.

        One test PER CLAUSE, not one "dirty metrics" test: a single case would
        pass with only one clause wired up, and the whole point of delegating
        to run_ingestion_benchmark._exit_code is that all of them arrive.
        """
        entries = [{"id": 1, "passed": True}]
        assert _exit_code(_report(entries)) == 0, "positive control"
        assert _exit_code(_report(entries, **override)) == 1


@pytest.fixture
def tiny_ground_truth(tmp_path):
    """A minimal 2-entry ground truth file exercising both the plain-query
    path and the seed+valid-from path, independent of the real fact_index.py
    fixture in evals/at_scale/query_ground_truth.json (which needs this
    repo's real history and isn't reproducible against a throwaway git_repo)."""
    gt = {
        "pinned_commit": "HEAD",
        "entries": [
            {
                "id": 1,
                "category": "point-in-time",
                "question": "How many commit entities exist?",
                "datalog": "[:find (count ?e) :where [?e :entity-type :type/commit]]",
                "expected": [[2]],
                "baseline_cmd": "python3 -c \"print(2)\"",
            },
            {
                "id": 2,
                "category": "cross-layer",
                "question": "Does the seeded decision exist?",
                "seed": "[[:decision/test-decision :entity-type :type/decision] [:decision/test-decision :description \"test\"]]",
                "seed_valid_from": "2020-01-01T00:00:00Z",
                "datalog": "[:find ?d :where [:decision/test-decision :description ?d]]",
                "expected": [["test"]],
                "baseline_cmd": "python3 -c \"print('test')\"",
            },
        ],
    }
    path = tmp_path / "ground_truth.json"
    path.write_text(json.dumps(gt))
    return path


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)
    return repo


class TestRunQueryBenchmark:
    @pytest.mark.asyncio
    async def test_all_entries_pass_against_matching_fixture(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert len(report["entries"]) == 2
        assert all(r["passed"] for r in report["entries"])

    @pytest.mark.asyncio
    async def test_result_includes_latency_fields(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        for r in report["entries"]:
            assert r["minigraf_latency_seconds"] >= 0
            assert r["baseline_latency_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_mismatch_reports_failure(self, git_repo, tmp_path, tiny_ground_truth):
        gt = json.loads(tiny_ground_truth.read_text())
        gt["entries"][0]["expected"] = [[999]]
        tiny_ground_truth.write_text(json.dumps(gt))
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        entries = report["entries"]
        assert entries[0]["passed"] is False
        assert entries[0]["actual"] != entries[0]["expected"]

    @pytest.mark.asyncio
    async def test_the_report_keeps_the_ingestion_metrics(self, git_repo, tmp_path, tiny_ground_truth):
        """#275: these were dropped on the floor. They are the only evidence
        that the graph the query numbers were measured over was built
        cleanly -- `processed` increments on the skip paths too and
        final_status stays "complete", so neither can see a dropped commit.
        """
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        ingestion = report["ingestion"]
        for key in (
            "skipped_commits", "error_signals", "stderr_capture_complete",
            "final_status", "commits_ingested",
        ):
            assert key in ingestion, key

    @pytest.mark.asyncio
    async def test_the_kept_metrics_exclude_only_poll_offsets(self, git_repo, tmp_path, tiny_ground_truth):
        """The one exclusion, and it must stay the only one: a hand-picked
        subset would drift the moment _exit_code grows a clause."""
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert "poll_offsets" not in report["ingestion"]
        assert "poll_duty_fraction" in report["ingestion"]

        from evals.at_scale.run_query_benchmark import _DROPPED_INGESTION_KEYS
        assert _DROPPED_INGESTION_KEYS == ("poll_offsets",)
