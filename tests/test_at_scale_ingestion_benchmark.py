# tests/test_at_scale_ingestion_benchmark.py
import subprocess as _subprocess

import pytest

from evals.at_scale.run_ingestion_benchmark import _exit_code, run_ingestion_benchmark


class TestExitCode:
    def test_zero_when_status_complete(self):
        assert _exit_code({"final_status": "complete"}) == 0

    def test_nonzero_when_status_error(self):
        assert _exit_code({"final_status": "error"}) == 1

    def test_zero_when_status_missing(self):
        assert _exit_code({}) == 0


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with two commits (mirrors tests/test_mcp_server.py's fixture)."""
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


class TestRunIngestionBenchmark:
    @pytest.mark.asyncio
    async def test_returns_expected_metric_keys(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert set(metrics.keys()) == {
            "repo_path", "branch", "commits_ingested", "wall_clock_seconds",
            "throughput_per_minute", "peak_rss_kb", "graph_size_bytes",
            "index_size_bytes", "status_latency", "query_latency", "final_status",
            "poll_count", "poll_duty_fraction", "poll_offsets",
        }

    @pytest.mark.asyncio
    async def test_poll_duty_fraction_is_a_bounded_fraction(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["poll_count"] == len(metrics["poll_offsets"])
        assert 0.0 <= metrics["poll_duty_fraction"] <= 1.0

    @pytest.mark.asyncio
    async def test_ingests_all_commits(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2
        assert metrics["final_status"] == "complete"

    @pytest.mark.asyncio
    async def test_wall_clock_and_sizes_are_positive(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["wall_clock_seconds"] > 0
        assert metrics["peak_rss_kb"] > 0
        assert metrics["graph_size_bytes"] > 0
        assert metrics["index_size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_default_branch_resolved_when_none_passed(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        # git_repo has no "main"/"master" branch name set explicitly by `git init`
        # in this sandbox's git config, so branch=None must still resolve to
        # something _run_ingestion can walk without raising.
        metrics = await run_ingestion_benchmark(str(git_repo), None, graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2


class TestCompareIgnore:
    @pytest.mark.asyncio
    async def test_ignore_comparison_present_when_requested(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05, compare_ignore=True
        )
        assert "ignore_comparison" in metrics
        comp = metrics["ignore_comparison"]
        assert comp["with_ignore_graph_size_bytes"] > 0
        assert comp["without_ignore_graph_size_bytes"] > 0
        assert comp["delta_bytes"] == (
            comp["without_ignore_graph_size_bytes"] - comp["with_ignore_graph_size_bytes"]
        )

    @pytest.mark.asyncio
    async def test_ignore_comparison_absent_by_default(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert "ignore_comparison" not in metrics


import asyncio
import time as _time

from evals.at_scale.run_ingestion_benchmark import _poll_during_ingestion


class TestPollerDoesNotStarveTheEventLoop:
    """#242: the poll must not block the event loop, and its share of
    _db_native_lock must stay bounded as the polled query grows."""

    @pytest.mark.asyncio
    async def test_event_loop_stays_responsive_while_poll_query_blocks(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: _time.sleep(0.05))

        ticks: list[float] = []

        async def heartbeat(stop: asyncio.Event) -> None:
            while not stop.is_set():
                ticks.append(_time.perf_counter())
                await asyncio.sleep(0.01)

        stop = asyncio.Event()
        ingest_task = asyncio.create_task(asyncio.sleep(0.6))
        hb = asyncio.create_task(heartbeat(stop))
        await _poll_during_ingestion(ingest_task, poll_interval=0.0, duty_factor=0.0)
        await ingest_task
        stop.set()
        await hb

        # A free 0.6s loop ticking every 10ms yields ~60 ticks. With the poll
        # blocking the loop for 50ms per iteration it yields ~12 (one per poll).
        # 30 sits well clear of both.
        assert len(ticks) >= 30, f"event loop was starved: only {len(ticks)} ticks"

    @pytest.mark.asyncio
    async def test_interval_backs_off_when_the_polled_query_is_slow(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: _time.sleep(0.05))

        ingest_task = asyncio.create_task(asyncio.sleep(1.1))
        _status, query_latencies, _offsets = await _poll_during_ingestion(
            ingest_task, poll_interval=0.0, duty_factor=10.0
        )
        await ingest_task

        # duty_factor=10 against a 50ms query forces a ~500ms sleep, so a 1.1s
        # run admits about 2-3 polls. Without the backoff it would poll
        # continuously and record ~20.
        assert len(query_latencies) <= 5, f"interval did not back off: {len(query_latencies)} polls"

    @pytest.mark.asyncio
    async def test_returns_poll_offsets_aligned_with_samples(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: None)

        ingest_task = asyncio.create_task(asyncio.sleep(0.2))
        status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
            ingest_task, poll_interval=0.02, duty_factor=10.0
        )
        await ingest_task

        assert len(poll_offsets) == len(status_latencies) == len(query_latencies)
        assert poll_offsets == sorted(poll_offsets)
