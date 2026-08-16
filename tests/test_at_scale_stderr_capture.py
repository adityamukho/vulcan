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
import subprocess
import sys
import time

from evals.at_scale.stderr_capture import scan_ingestion_stderr, tee_stderr

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
        with contextlib.suppress(ValueError):
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
