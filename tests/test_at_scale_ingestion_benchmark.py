# tests/test_at_scale_ingestion_benchmark.py
import contextlib
import io
import os
import subprocess as _subprocess
import sys
from pathlib import Path

import pytest

import evals.at_scale.run_ingestion_benchmark as rib
from evals.at_scale.run_ingestion_benchmark import (
    _exit_code,
    resolve_graph_path,
    run_ingestion_benchmark,
)
from evals.at_scale.stderr_capture import TeeStderrFailure

# Every key run_ingestion_benchmark returns on a clean run. Shared with the
# tee-failure tests below, whose whole point is that the failure path produces
# the SAME dict plus its two diagnostic keys, not a truncated one.
_EXPECTED_METRIC_KEYS = {
    "repo_path", "branch", "graph_path", "commits_ingested", "wall_clock_seconds",
    "throughput_per_minute", "peak_rss_kb", "graph_size_bytes",
    "index_size_bytes", "status_latency", "query_latency", "final_status",
    "poll_count", "poll_duty_fraction", "poll_offsets", "checkpoint_summary",
    "skipped_commits", "error_signals", "correction_sweep_summaries",
    "correction_sweep_skipped", "stderr_capture_complete",
}


class TestExitCode:
    def test_zero_when_status_complete(self):
        assert _exit_code({"final_status": "complete"}) == 0

    def test_nonzero_when_status_error(self):
        assert _exit_code({"final_status": "error"}) == 1

    def test_zero_when_status_missing(self):
        assert _exit_code({}) == 0


class TestExitCodeGate:
    def test_clean_run_exits_zero(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 0

    def test_a_skipped_commit_fails_the_run(self):
        """processed and final_status are both blind to this -- the gate is
        the only thing that turns a dropped commit into a failure."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": ["abc123"],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 1

    def test_a_251_signature_fails_the_run(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [{"pattern": "page_out_of_bounds", "line": "..."}],
            "stderr_capture_complete": True,
        }) == 1

    def test_error_status_still_fails(self):
        assert _exit_code({
            "final_status": "error",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 1

    def test_missing_keys_are_treated_as_clean_for_old_metrics(self):
        """Pre-#256 metrics files carry none of these keys; _exit_code must
        not crash reading them, and must not fail them either."""
        assert _exit_code({"final_status": "complete"}) == 0

    def test_a_tee_failure_fails_the_run(self):
        """run_ingestion_benchmark CATCHES TeeStderrFailure so a broken tee
        does not destroy a 25-minute run's metrics. Without this clause that
        catch silently converts the failure back into exit 0."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": False,
            "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
        }) == 1

    def test_an_incomplete_capture_fails_even_with_empty_signal_lists(self):
        """The discriminating case: a truncated capture yields EMPTY
        skipped_commits/error_signals, which are byte-identical to a clean
        run's. They are lower bounds, so the emptiness proves nothing and the
        flag alone must fail the run -- even with no tee_failure string."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": False,
        }) == 1

    def test_a_complete_capture_with_no_signals_is_still_clean(self):
        """Counterpart to the above: the flag must fail only when explicitly
        False, or it would fail every clean run too."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 0


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
        assert set(metrics.keys()) == _EXPECTED_METRIC_KEYS

    @pytest.mark.asyncio
    async def test_records_the_resolved_graph_path(self, git_repo, tmp_path):
        """#256 review round 5. The probe recording the path it was HANDED
        says nothing about whether that was the right graph; the pairing is
        only auditable if the metrics record their own side of it. Resolved,
        so a relative invocation still pairs with the probe's resolved path.
        """
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )
        assert metrics["graph_path"] == str(graph_path.resolve())

    @pytest.mark.asyncio
    async def test_a_clean_run_reports_a_complete_capture_and_no_signals(self, git_repo, tmp_path):
        """The tee spans the whole run, so a healthy two-commit ingestion must
        come back with a complete capture and empty signal lists -- and pass
        the gate."""
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["stderr_capture_complete"] is True
        assert metrics["skipped_commits"] == []
        assert metrics["error_signals"] == []
        assert metrics["correction_sweep_skipped"] == 0
        assert "tee_failure" not in metrics
        assert _exit_code(metrics) == 0

    @pytest.mark.asyncio
    async def test_checkpoint_summary_is_present_and_self_consistent(self, git_repo, tmp_path):
        """#241 Task 6: the benchmark's acceptance criterion is a run that
        reports realised checkpoint duty, not just poll duty. mcp_server
        publishes this into _ingest_progress["checkpoint_summary"] just
        before _run_ingestion discards the policy that held the counters;
        the harness must carry it through into its own returned metrics."""
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        summary = metrics["checkpoint_summary"]
        assert summary is not None
        assert summary["checkpoints"] >= 1
        assert summary["total_seconds"] >= 0.0
        if summary["elapsed_seconds"] > 0:
            assert summary["realised_duty"] == pytest.approx(
                summary["total_seconds"] / summary["elapsed_seconds"]
            )

    # test_poll_duty_fraction_is_a_bounded_fraction was DELETED here (final
    # whole-branch review). Both of its assertions were tautologies:
    # poll_count is *defined* as len(poll_offsets)
    # (run_ingestion_benchmark.py:171), and poll_duty_fraction is a serial sum
    # of latencies measured INSIDE the same wall_clock it divides by
    # (:148-149), so 0.0 <= f <= 1.0 cannot fail. It had zero discriminating
    # power while being counted as #242 coverage -- the exact pattern this
    # project has already shipped four times. Its only non-vacuous content
    # (key presence) is covered by test_returns_expected_metric_keys above,
    # and the real offsets/samples alignment by
    # TestPollerDoesNotStarveTheEventLoop::test_returns_poll_offsets_aligned_with_samples,
    # which is ablation-proven.

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


def _emit_on_fd_2_after_ingesting(monkeypatch, payload: bytes):
    """Wrap mcp_server._run_ingestion so it runs for real and then writes
    `payload` to fd 2.

    os.write(2, ...), NOT print(file=sys.stderr): under pytest's fd capture
    sys.stderr is not fd 2 at all, so a print would never enter the tee pipe
    and the test would pass on a permanently blind wiring -- the exact
    fail-open shape being tested against. mcp_server's real skip sites do
    reach fd 2 (print(file=sys.stderr) with no capture in front of it, plus
    the pool children which inherit fd 2 and have no sys.stderr of the
    parent's at all), which is why the tee is fd-level in the first place.
    """
    import mcp_server

    real_run_ingestion = mcp_server._run_ingestion

    async def run_then_emit(*args, **kwargs):
        await real_run_ingestion(*args, **kwargs)
        os.write(2, payload)

    monkeypatch.setattr(mcp_server, "_run_ingestion", run_then_emit)


class TestCapturedStderrReachesTheMetrics:
    """The POSITIVE direction of this task's claim: a line emitted on fd 2
    during a tee'd run must actually arrive in the metrics.

    Every other test here is satisfied by a permanently blind wiring --
    scan_ingestion_stderr("") also yields empty lists, and a clean run's
    empty lists are byte-identical to a blind scanner's. These two are the
    only tests that can tell the difference, so they are what stops the
    verification from failing open."""

    @pytest.mark.asyncio
    async def test_a_real_skip_line_on_fd_2_reaches_skipped_commits(
        self, git_repo, tmp_path, monkeypatch
    ):
        _emit_on_fd_2_after_ingesting(
            monkeypatch,
            b"[_run_ingestion] skipping commit deadbee1 "
            b"('some subject'): write failed: boom\n",
        )
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert metrics["skipped_commits"] == ["deadbee1"]
        assert metrics["stderr_capture_complete"] is True
        # final_status and commits_ingested are both blind to this -- the
        # scanned keys are the only thing that turns it into a failure.
        assert metrics["final_status"] == "complete"
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_a_real_251_signature_on_fd_2_reaches_error_signals(
        self, git_repo, tmp_path, monkeypatch
    ):
        # The live-numbers form #251 actually reproduced; a literal-string
        # scanner would match nothing here and report all-clear.
        _emit_on_fd_2_after_ingesting(
            monkeypatch, b"Page 130 out of bounds (total pages: 113)\n"
        )
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert [s["pattern"] for s in metrics["error_signals"]] == ["page_out_of_bounds"]
        assert "total pages: 113" in metrics["error_signals"][0]["line"]
        assert metrics["stderr_capture_complete"] is True
        assert _exit_code(metrics) == 1


class TestTeeFailureDoesNotDestroyTheRun:
    """tee_stderr() raises TeeStderrFailure from its own teardown, which runs
    AFTER _run_ingestion has returned. If run_ingestion_benchmark let that
    propagate, a ~25-minute run would end in a traceback with no metrics JSON
    and no report row -- the instrument's failure destroying the measurement.
    It must instead produce the full metrics dict, flagged and failing."""

    @staticmethod
    def _failing_tee(exc_factory=lambda: TeeStderrFailure("simulated pump failure")):
        """A tee_stderr() stand-in that captures for real, then raises on exit
        exactly where the real one does -- in its own finally, after the body
        has completed."""
        real_tee = rib.tee_stderr

        @contextlib.contextmanager
        def failing_tee():
            try:
                with real_tee() as capture:
                    yield capture
            finally:
                raise exc_factory()

        return failing_tee

    @pytest.mark.asyncio
    async def test_full_metrics_survive_a_tee_failure(self, git_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        # Nothing is actually lost on this path: the raise lands after
        # _run_ingestion returned, so every metric below was still readable.
        assert _EXPECTED_METRIC_KEYS <= set(metrics)
        assert metrics["commits_ingested"] == 2
        assert metrics["final_status"] == "complete"
        assert metrics["wall_clock_seconds"] > 0
        assert metrics["graph_size_bytes"] > 0
        assert metrics["checkpoint_summary"] is not None

    @pytest.mark.asyncio
    async def test_a_tee_failure_is_flagged_and_fails_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert metrics["stderr_capture_complete"] is False
        assert "simulated pump failure" in metrics["tee_failure"]
        # The lists are lower bounds here, so their emptiness must NOT read as
        # a clean run. This is the whole point of the third _exit_code clause.
        assert metrics["skipped_commits"] == []
        assert metrics["error_signals"] == []
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_a_body_exception_displaced_by_the_tee_raise_is_preserved(
        self, git_repo, tmp_path, monkeypatch
    ):
        """A raise from the context manager's finally DISPLACES whatever the
        body was raising: the real ingestion crash comes out only as
        TeeStderrFailure.__context__. A handler that recorded the tee failure
        alone would hide a genuine crash behind an instrument fault."""
        import mcp_server

        async def boom(*_args, **_kwargs):
            raise RuntimeError("ingestion exploded")

        monkeypatch.setattr(mcp_server, "_run_ingestion", boom)
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert "ingestion exploded" in metrics["tee_failure_context"]
        assert metrics["stderr_capture_complete"] is False
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_the_handlers_own_logging_cannot_destroy_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        """The handler logs to stderr -- and on the one path that matters
        most, that stderr is broken. When guard.restore()'s dup2 is what
        failed, fd 2 still points at the tee pipe, whose read end teardown
        then closes, so every write raises BrokenPipeError. Unguarded, that
        propagates out of the except clause and destroys the metrics dict the
        catch exists to preserve."""

        class _RecordingStderr:
            """Genuinely fd 2 (so the BrokenPipeError comes from the OS, not
            from a stub), while recording that it really did raise -- without
            that positive control this test would pass vacuously in any
            environment where the write happened to succeed."""

            def __init__(self) -> None:
                self._raw = io.TextIOWrapper(
                    io.FileIO(2, "w", closefd=False), line_buffering=True
                )
                self.raised: list[BaseException] = []

            def write(self, s):
                try:
                    return self._raw.write(s)
                except BaseException as exc:
                    self.raised.append(exc)
                    raise

            def flush(self):
                try:
                    return self._raw.flush()
                except BaseException as exc:
                    self.raised.append(exc)
                    raise

        real_tee = rib.tee_stderr

        @contextlib.contextmanager
        def tee_that_fails_to_restore_fd_2():
            with real_tee() as capture:
                yield capture
            # Exactly the state a failed guard.restore() leaves behind: fd 2
            # points at a pipe whose read end is already closed. Done AFTER
            # the real tee's teardown has joined its pump, so nothing can
            # repair it behind our back -- the reviewer's reproduction was
            # racy against the pump's emergency valve; this one is not.
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            os.dup2(write_fd, 2)
            os.close(write_fd)
            raise TeeStderrFailure("simulated restore failure")

        rescue_fd = os.dup(2)
        recorder = _RecordingStderr()
        monkeypatch.setattr(rib, "tee_stderr", tee_that_fails_to_restore_fd_2)
        monkeypatch.setattr(sys, "stderr", recorder)
        graph_path = tmp_path / "bench.graph"

        try:
            metrics = await rib.run_ingestion_benchmark(
                str(git_repo), "HEAD", graph_path, poll_interval=0.05
            )
        finally:
            # Repair fd 2 before pytest (or anything else) writes to it.
            os.dup2(rescue_fd, 2)
            os.close(rescue_fd)

        assert any(isinstance(exc, BrokenPipeError) for exc in recorder.raised), (
            "the handler's stderr never actually broke, so this test proved "
            f"nothing: {recorder.raised}"
        )
        assert _EXPECTED_METRIC_KEYS <= set(metrics)
        assert metrics["commits_ingested"] == 2
        assert metrics["stderr_capture_complete"] is False
        assert _exit_code(metrics) == 1


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


class TestResolveGraphPath:
    def test_without_an_argument_yields_a_temp_path_that_is_cleaned_up(self):
        """Omitting --graph-path must behave exactly as before this change --
        the recurring benchmark must not change."""
        with resolve_graph_path(None) as path:
            tmpdir = path.parent
            assert tmpdir.exists()
            assert path.name == "bench.graph"
        assert not tmpdir.exists()

    def test_with_an_argument_yields_that_path_and_keeps_it(self, tmp_path):
        target = tmp_path / "persistent" / "run.graph"
        with resolve_graph_path(str(target)) as path:
            assert path == target
            path.write_text("graph bytes")
        assert target.exists(), "a persistent graph must survive the context"

    def test_creates_the_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "run.graph"
        with resolve_graph_path(str(target)) as path:
            assert path.parent.is_dir()

    def test_refuses_an_existing_path(self, tmp_path):
        """CLAUDE.md's standing rule: graphs are rebuilt, never re-ingested in
        place. run_ingestion_benchmark's docstring states the same
        precondition; this enforces it."""
        target = tmp_path / "already.graph"
        target.write_text("pre-existing")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass

    def test_refuses_a_stale_wal_even_when_the_main_file_is_absent(self, tmp_path):
        """A crashed run can leave `<path>.wal` behind with the main graph
        file deleted (or never renamed into place). minigraf's open()
        replays a leftover .wal automatically, so a check that only looks at
        the main file would silently resurrect the dead run's writes."""
        target = tmp_path / "run.graph"
        Path(f"{target}.wal").write_text("stale wal")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass

    def test_refuses_a_stale_index_even_when_the_main_file_is_absent(self, tmp_path):
        """Same hazard as the .wal case, for the fact index sidecar. Uses
        fact_index.index_path_for so this stays correct under a
        MINIGRAF_INDEX_PATH override, same as the implementation."""
        import fact_index

        target = tmp_path / "run.graph"
        Path(fact_index.index_path_for(str(target))).write_text("stale index")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass
