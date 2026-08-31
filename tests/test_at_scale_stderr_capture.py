"""Unit tests for the #256 stderr instrumentation.

The scanner is where this verification can fail OPEN: a scanner with a broken
pattern reports "no errors found" having matched nothing, which is
indistinguishable from a healthy run. So every pattern carries a positive
control, not just the clean-log negative control.

Fixtures are plain string literals on purpose. Building them through tmp_path
would bake each test's own name into the text, letting the scanner match the
path instead of the planted line.
"""

import concurrent.futures
import contextlib
import io
import multiprocessing
import os
import selectors
import subprocess
import sys
import time

import pytest

from evals.at_scale.stderr_capture import TeeStderrFailure, scan_ingestion_stderr, tee_stderr

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

    def test_detects_the_tee_stderr_pump_failed_marker(self):
        """Positive control for the fourth _ERROR_PATTERNS entry (#256
        review round 3): tee_stderr()'s own pump-failure marker is the ONLY
        signal left in the captured text once the pump has died, so this
        scanner must not treat a marker-only capture as a clean run."""
        text = "[tee_stderr] pump failed: BrokenPipeError(32, 'Broken pipe')\n"
        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["tee_stderr_pump_failed"]

    def test_detects_a_run_level_ingestion_failure(self):
        """#270. The other four patterns are per-commit or apparatus
        signatures; none of them fires when the RUN dies -- a failure before
        Stage A skips no commit, corrupts no page, and breaks no pump, so
        every existing signal reads clean while the run produced nothing.
        This is the fifth entry, and it is the one that covers the whole
        window before _run_ingestion builds its _CheckpointPolicy."""
        text = "[_run_ingestion] ingestion failed: RuntimeError: boom\n"
        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["ingestion_failed"]

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


_CHILD_WRITES_TO_FD2 = "import os; os.write(2, b'CHILD_MARKER\\n')"


class TestTeeStderr:
    def test_captures_parent_process_writes(self):
        # os.write(2, ...), not print(file=sys.stderr): pytest's own default
        # capturing monkeypatches sys.stderr to a non-fd object (see the
        # ablation below), so a print() here would be invisible to fd-level
        # capture for the same reason the ablation exists -- it would test
        # pytest's capture layering, not tee_stderr.
        with tee_stderr() as cap:
            os.write(2, b"PARENT_MARKER\n")
        assert "PARENT_MARKER" in cap.text()

    def test_output_still_reaches_real_stderr(self, capfd):
        """A tee that swallowed a 25-minute run's live output would be worse
        than no tee."""
        with tee_stderr() as cap:
            os.write(2, b"PASSTHROUGH_MARKER\n")
        assert "PASSTHROUGH_MARKER" in cap.text()
        assert "PASSTHROUGH_MARKER" in capfd.readouterr().err

    def test_ablation_fd_tee_catches_child_output_a_sys_stderr_swap_misses(self):
        """THE ablation for this design choice.

        _extract_commit runs in a ProcessPoolExecutor whose workers inherit
        fd 2, not the parent's sys.stderr object, and the #251 strings are
        minigraf's own Rust strings. If either arrives as a native write
        rather than a caught Python exception, only fd-level capture sees it.

        If the first assertion FAILS, the sys.stderr swap is sufficient and
        the fd-level machinery is not justified -- fall back to the swap.
        """
        swap_buf = io.StringIO()
        with contextlib.redirect_stderr(swap_buf):
            subprocess.run([sys.executable, "-c", _CHILD_WRITES_TO_FD2], check=True)
        assert "CHILD_MARKER" not in swap_buf.getvalue(), (
            "the sys.stderr swap saw child fd-2 output -- the ablation does "
            "not hold and the fd-level tee is unjustified"
        )

        with tee_stderr() as cap:
            subprocess.run([sys.executable, "-c", _CHILD_WRITES_TO_FD2], check=True)
        assert "CHILD_MARKER" in cap.text()

    def test_restores_fd_2_on_exit(self):
        before = os.fstat(2)
        with tee_stderr():
            pass
        after = os.fstat(2)
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    def test_restores_fd_2_even_when_the_body_raises(self):
        before = os.fstat(2)
        # pytest.raises, not contextlib.suppress: suppress() passes whether the
        # body exception propagated or tee_stderr swallowed it, so a tee that
        # ate the caller's exception would score as a pass here.
        with pytest.raises(ValueError, match="boom"):
            with tee_stderr():
                raise ValueError("boom")
        after = os.fstat(2)
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    def test_join_is_load_bearing_for_a_large_write(self, capfd):
        """Regression test for #256 review round 1, Important 4: dropping or
        no-op'ing pump_thread.join() lets the `with` block return control to
        the caller before the pump has finished draining the pipe. A short
        marker barely exercises this (the pump usually wins the race by
        luck); a write past the 64 KiB pipe buffer does not fit in one
        os.read(), so it reliably needs more than one pump iteration and
        therefore reliably needs the join to actually wait. Ablation-proven:
        see the #256 fix-round-1 report for the exact pass/fail counts with
        and without the join.

        Round 2 addition: also assert the passthrough half got the whole
        payload, not just the capture -- "never swallow the run's live
        output" (test_output_still_reaches_real_stderr's docstring) applies
        to large writes too, and nothing previously checked that.
        """
        payload = b"L" * (128 * 1024)  # 128 KiB, comfortably > the 64 KiB pipe buffer
        with tee_stderr() as cap:
            os.write(2, payload)
        assert cap.text() == payload.decode("ascii")
        assert cap.errors == []
        assert payload.decode("ascii") in capfd.readouterr().err

    def test_shutdown_does_not_wait_for_eof_with_a_spawn_pool(self):
        """THE discriminating regression test for the Critical-1 fix (#256
        review round 2, New Important/coverage): all other tests in this
        file pass even with the control-pipe mechanism reverted to the old
        EOF design, because none of them involve a spawn-context pool whose
        resource_tracker inherits fd 2 and outlives the pool. Ablation-
        proven against that revert: see the #256 fix-round-2 report for the
        exact pass/fail counts.
        """
        ctx = multiprocessing.get_context("spawn")
        t0 = time.perf_counter()
        with tee_stderr() as cap:
            ex = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)
            ex.submit(os.getpid).result()
            ex.shutdown(wait=True)
            os.write(2, b"AFTER_POOL\n")
        assert time.perf_counter() - t0 < 5  # 10s on the EOF design
        assert "AFTER_POOL" in cap.text() and cap.errors == []

    def test_selector_construction_failure_is_surfaced_not_swallowed(self, monkeypatch):
        """THE discriminating regression test for the round-3 Critical fix,
        and the coverage gap the review called out directly: nothing
        previously tested finding 2 (the marker + TeeStderrFailure) at all.

        In round 2, selectors.DefaultSelector() and both register() calls
        sat OUTSIDE the pump's try -- so a failure there (e.g. OSError
        (EMFILE) from epoll allocation, exactly the fd-pressure condition
        that motivated switching to selectors in the first place) skipped
        the except clause entirely: no marker, no record_error, no
        emergency_redirect, and the `with` block exited silently with
        cap.text() == "" -- a byte-identical false all-clear. Round 3 moved
        the construction inside the try; this test pins that directly by
        forcing the construction itself to fail. Ablation-proven: see the
        #256 fix-round-3 report for the exact pass/fail counts with the
        construction moved back outside the try.
        """

        def raising_selector():
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(selectors, "DefaultSelector", raising_selector)

        cap = None
        with pytest.raises(TeeStderrFailure):
            with tee_stderr() as cap:
                pass

        assert cap is not None
        # The PLANTED error, not merely "an OSError" (#256 review round 4,
        # Important 2): TimeoutError is an OSError subclass, so teardown's own
        # join-timeout TimeoutError satisfied the loose form -- the test would
        # have passed on a run where the planted failure never happened at all.
        assert [
            exc for exc in cap.errors if getattr(exc, "errno", None) == 24
        ], f"planted EMFILE not recorded; errors={cap.errors!r}"
        # The marker must carry the exception, not just the prefix: dropping
        # {exc!r} from the emitter would keep a prefix-only assertion green
        # while destroying the only diagnostic a text-only consumer gets.
        assert "[tee_stderr] pump failed:" in cap.text()
        assert "Too many open files" in cap.text()
        # The missing link: assert the marker the pump ACTUALLY emits is seen
        # by the pattern that ACTUALLY exists. Everything above this line is a
        # hand-written literal agreeing with the emitter by eye only.
        assert scan_ingestion_stderr(cap.text())["error_signals"], cap.text()

    def test_a_mid_run_pump_death_is_still_scannable(self, monkeypatch):
        """THE discriminating regression test for #256 review round 4,
        Important 1: the emitter defeating the scanner's own anchor.

        Every earlier test killed the pump at iteration 0, where the marker
        happens to land at the start of the capture. Mid-run EMFILE under fd
        pressure is the LIKELIER timing, and there the marker was appended
        straight after an arbitrary 64 KiB os.read() slice that need not end
        at a line boundary -- gluing it onto a partial line, which the then-
        anchored `^\\[tee_stderr\\]` pattern did not match. A pump death
        scanned as a clean run: the exact fail-open shape #256 exists to
        catch.

        Two independent halves of the fix, and this test discriminates both.
        Ablation-proven -- see the #256 fix-round-4 report for the counts:
        removing the emitter's leading newline breaks the "marker is its own
        line" assertion; restoring the `^` anchor breaks the final
        concatenated-text assertion.
        """
        real_default_selector = selectors.DefaultSelector

        class _FailsOnTheSecondSelect:
            """A real selector for one iteration, then EMFILE. Real, not a
            stub: the pump must genuinely read and append one chunk first, or
            this degenerates into the iteration-0 case again."""

            def __init__(self) -> None:
                self._real = real_default_selector()
                self._selects = 0

            def register(self, *args, **kwargs):
                return self._real.register(*args, **kwargs)

            def select(self, *args, **kwargs):
                self._selects += 1
                if self._selects > 1:
                    raise OSError(24, "Too many open files")
                return self._real.select(*args, **kwargs)

            def close(self) -> None:
                self._real.close()

        # Patched before entry: the pump builds its selector as soon as the
        # thread starts, which is inside tee_stderr()'s setup.
        monkeypatch.setattr(selectors, "DefaultSelector", _FailsOnTheSecondSelect)

        cap = None
        with pytest.raises(TeeStderrFailure):
            with tee_stderr() as cap:
                # Deliberately no trailing newline -- os.read() returns
                # whatever fits in 64 KiB, so a partial final line is the
                # normal case, not a contrived one. The pump's select #1
                # blocks until this arrives, reads it, loops, and dies on
                # select #2: mid-run by construction, not by timing luck.
                os.write(2, b"[_run_ingestion] MID_RUN_PARTIAL")

        assert cap is not None
        text = cap.text()
        assert "MID_RUN_PARTIAL" in text, "the pump never got mid-run"

        signals = scan_ingestion_stderr(text)["error_signals"]
        assert [s["pattern"] for s in signals] == ["tee_stderr_pump_failed"], text
        marker_line = signals[0]["line"]
        assert marker_line.startswith("[tee_stderr] pump failed:"), (
            "the marker was glued onto the preceding partial line instead of "
            f"starting one of its own: {text!r}"
        )
        assert "Too many open files" in marker_line

        # Feed the pump's OWN marker back in, concatenated onto a partial
        # line. The pattern must not depend on the emitter's newline: any
        # consumer can receive this text already merged with another writer's
        # output on the shared fd.
        glued = "[_run_ingestion] a partial line with no trailing newline" + marker_line
        assert scan_ingestion_stderr(glued)["error_signals"], (
            "the pattern only matches the marker at a line start -- an anchor "
            "the emitter can defeat"
        )


class TestFailingRestoreIsRecorded:
    """`guard.restore()` is the one teardown step whose failure means fd 2 was
    never handed back -- the most consequential thing this module can report.
    Its exception is raised INSIDE teardown's try/finally, so the
    TeeStderrFailure raised at the bottom of that same finally displaces it
    into __context__ only. Unless it is also recorded on the capture, a
    consumer logging str(exc) (or reading cap.errors) never sees it."""

    def test_a_failing_restore_survives_a_compound_failure(self, monkeypatch):
        """The COMPOUND case, which is the only one where the restore failure
        is genuinely lost: the pump has also died, so capture.errors is
        already non-empty and teardown's finally raises TeeStderrFailure --
        which DISPLACES the propagating restore OSError into __context__.
        With a dead pump alone in .errors, str(exc) names only the pump."""
        real_default_selector = selectors.DefaultSelector

        class _FailsImmediately:
            def __init__(self) -> None:
                self._real = real_default_selector()

            def register(self, *args, **kwargs):
                return self._real.register(*args, **kwargs)

            def select(self, *args, **kwargs):
                raise OSError(24, "Too many open files")

            def close(self) -> None:
                self._real.close()

        # Our own copy of the real stderr, taken before anything is patched. A
        # failed restore can leave fd 2 pointing at the tee pipe whose read end
        # teardown then closes, so every later write to stderr -- including
        # pytest's own -- would raise BrokenPipeError. Repaired below, before
        # any assertion runs.
        rescue_fd = os.dup(2)
        real_dup2 = os.dup2
        armed = {"value": False}

        def flaky_dup2(fd, fd2, *args, **kwargs):
            if armed["value"] and fd2 == 2:
                raise OSError(9, "simulated restore failure")
            return real_dup2(fd, fd2, *args, **kwargs)

        monkeypatch.setattr(selectors, "DefaultSelector", _FailsImmediately)
        monkeypatch.setattr(os, "dup2", flaky_dup2)

        cap = None
        try:
            with pytest.raises(TeeStderrFailure) as excinfo:
                with tee_stderr() as cap:
                    # Armed only now: setup's own dup2(write_fd, 2) must
                    # succeed, or this tests the setup path instead.
                    armed["value"] = True
        finally:
            armed["value"] = False
            real_dup2(rescue_fd, 2)
            os.close(rescue_fd)

        assert cap is not None
        assert "Too many open files" in str(excinfo.value), "the pump did not die"
        assert "simulated restore failure" in str(excinfo.value), (
            "the restore failure -- fd 2 was never handed back, the worst thing "
            "this module can report -- is reachable only via __context__, so a "
            f"consumer logging str(exc) loses it: {excinfo.value}"
        )
        assert any(
            "simulated restore failure" in repr(err) for err in cap.errors
        ), f"not recorded on the capture either: {cap.errors}"
