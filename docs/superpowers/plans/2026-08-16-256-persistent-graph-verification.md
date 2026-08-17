# #256 Persistent-Graph Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Confirm #251 (two live `MiniGrafDb` handles corrupting each other) does not recur, via an at-scale ingestion against a graph that still exists when the run ends.

**Architecture:** `run_ingestion_benchmark.py` gains two generic capabilities — a `--graph-path` that persists the graph, and a file-descriptor-level stderr tee whose output is scanned into the metrics JSON. A separate probe process then opens the surviving graph and cross-checks the provisional residue (M) against the correction sweep's own skip accounting (N), asserting `M <= N`. The instrumentation carries the tests; the run itself is an observation.

**Tech Stack:** Python 3.11+, pytest, minigraf (Rust-backed graph store), `os.dup2`-based fd capture.

**Spec:** `docs/superpowers/specs/2026-08-16-256-persistent-graph-verification-design.md`

## Global Constraints

- **Always use `.venv/bin/python`.** System python has minigraf 1.1.1 against a `minigraf>=1.2.3` floor; it fakes ~122 test failures and makes queries ~7x slower. Every command in this plan uses `.venv/bin/python`.
- **No changes to `mcp_server.py`.** Both N and the skip lines are read from stderr the production code already emits. If a task seems to need an `mcp_server.py` edit, stop and re-read the spec.
- **Do not touch `_lease_manager` or its tests.** #272's instance-level-monkeypatch trap (`tests/test_mcp_server.py:288`, workaround at 23331) is out of scope and stays open.
- **Single-handle invariant:** at most one live `MiniGrafDb` handle per process. The probe is a separate process for exactly this reason.
- **Never commit the graph.** It is ~211 MB plus an ~89 MB index. Committed artifacts are the two JSON files and the `benchmark.md` entry only.
- **Commit messages must not contain closing keywords** (`close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved`) before a `#N`. Use `Refs #256`. Verify with `git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?"` after each commit.
- Branch: `fix-256-persistent-graph-verification` (already created; spec committed at c9f18b9).

## File Structure

| File | Responsibility |
|---|---|
| Create `evals/at_scale/stderr_capture.py` | The fd-level tee and the stderr scanner. Small focused module alongside `metrics.py` / `report.py`, matching their pattern. |
| Create `tests/test_at_scale_stderr_capture.py` | Scanner positive/negative controls; the tee ablation. |
| Modify `evals/at_scale/run_ingestion_benchmark.py` | `--graph-path`, tee wiring, three new metrics keys, extended `_exit_code`. |
| Modify `tests/test_at_scale_ingestion_benchmark.py` | Path-resolution and exit-code tests. |
| Create `evals/at_scale/probe_provisional_residue.py` | Pure analysis primitives + graph query + CLI. |
| Create `tests/test_at_scale_provisional_residue_probe.py` | Primitives unit tests; planted-marker real-graph test. |

---

### Task 1: The stderr scanner

Pure text-in, dict-out. No I/O, so it is fully unit-testable and is where the fail-open risk lives.

**Files:**
- Create: `evals/at_scale/stderr_capture.py`
- Test: `tests/test_at_scale_stderr_capture.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `scan_ingestion_stderr(text: str) -> dict[str, Any]` returning keys `skipped_commits: list[str]`, `error_signals: list[dict[str, str]]`, `correction_sweep_summaries: list[int]`, `correction_sweep_skipped: int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_at_scale_stderr_capture.py`. Note these fixtures are plain string literals — deliberately **not** built through `tmp_path`. A `tmp_path` string carries the test's own name, and a test named e.g. `test_page_out_of_bounds` can make the scanner match on the path rather than the planted line, passing for the wrong reason.

```python
# tests/test_at_scale_stderr_capture.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_stderr_capture.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'evals.at_scale.stderr_capture'`.

- [ ] **Step 3: Write the scanner**

Create `evals/at_scale/stderr_capture.py`:

```python
# evals/at_scale/stderr_capture.py
"""Stderr capture and scanning for the at-scale ingestion benchmark (#256).

_run_ingestion isolates per-commit failures rather than propagating them (its
documented "fail only the one commit" contract), and _ingest_progress
["processed"] increments on the skip paths too. So neither `processed` nor
`final_status` can tell you a commit was dropped -- the stderr line is the
only signal. Same for the correction sweep's residue total.
"""

from __future__ import annotations

import re
from typing import Any

# Both skip sites in _run_ingestion's per-commit loop (mcp_server.py:11106
# and 11152). One regex covers both; the optional "unreadable " is the
# extraction-phase variant.
_SKIPPED_COMMIT_RE = re.compile(
    r"^\[_run_ingestion\] skipping (?:unreadable )?commit (\S+)",
    re.MULTILINE,
)

# _correction_sweep_log_summary (mcp_server.py:10590). UNCAPPED, unlike the
# per-entity logs (_CORRECTION_SWEEP_LOG_CAP = 10), which is what makes it
# usable as an accounting total.
_SWEEP_SUMMARY_RE = re.compile(
    r"^\[_correction_sweep\] (\d+) entities left provisional/unreconciled this run",
    re.MULTILINE,
)

# The three #251 signatures. REGEXES, NOT LITERALS: the page error carries
# live numbers ("Page 130 out of bounds (total pages: 113)" is the form #251
# actually reproduced), so a literal would match nothing and report all-clear.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("page_out_of_bounds", re.compile(r"Page \d+ out of bounds")),
    ("serde_deserialization_error", re.compile(r"Serde Deserialization Error")),
    (
        "stream_all_entries_expected_leaf_page",
        re.compile(r"stream_all_entries: expected leaf page"),
    ),
)


def scan_ingestion_stderr(text: str) -> dict[str, Any]:
    """Scan captured ingestion stderr for dropped commits, #251 signatures,
    and the correction sweep's residue total.

    correction_sweep_skipped defaults to 0 when no summary line is present.
    That is not "unmeasured" -- _correction_sweep_log_summary prints only
    `if skipped_events:`, so an absent line genuinely means zero. The key is
    therefore ALWAYS present; a consumer that finds it missing is looking at
    a metrics file written before this instrumentation existed and must fail
    rather than assume.

    Two loops call _correction_sweep_log_summary, so a run can emit more than
    one summary. Every value is kept in correction_sweep_summaries; the
    headline correction_sweep_skipped is their max -- the smallest defensible
    N, which keeps `M <= N` as strict as the evidence allows.
    """
    error_signals: list[dict[str, str]] = []
    for line in text.splitlines():
        for name, pattern in _ERROR_PATTERNS:
            if pattern.search(line):
                error_signals.append({"pattern": name, "line": line.strip()})

    summaries = [int(n) for n in _SWEEP_SUMMARY_RE.findall(text)]

    return {
        "skipped_commits": _SKIPPED_COMMIT_RE.findall(text),
        "error_signals": error_signals,
        "correction_sweep_summaries": summaries,
        "correction_sweep_skipped": max(summaries) if summaries else 0,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_stderr_capture.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/stderr_capture.py tests/test_at_scale_stderr_capture.py
git commit -m "Add the #256 ingestion stderr scanner

processed and final_status are both blind to dropped commits -- the
per-commit handler isolates failures and increments processed anyway -- so
the stderr line is the only signal that a commit was skipped.

The three #251 signatures are regexes, not literals: the page error carries
live numbers, and a literal would match nothing while reporting all-clear.
Every pattern gets a positive control for that reason.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 2: The fd-level stderr tee, with its ablation

The spec justifies fd-level capture over a `sys.stderr` swap with a counterfactual claim. That claim gets an experiment, not an assumption. **If the ablation does not reproduce the miss, stop and fall back to the simpler `sys.stderr` swap** — the complexity is then unjustified.

**Files:**
- Modify: `evals/at_scale/stderr_capture.py`
- Test: `tests/test_at_scale_stderr_capture.py`

**Interfaces:**
- Consumes: nothing from Task 1 (same module, independent function).
- Produces: `tee_stderr()` — a context manager yielding an object with `.text() -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_stderr_capture.py`:

```python
import contextlib
import io
import os
import subprocess
import sys

from evals.at_scale.stderr_capture import tee_stderr

_CHILD_WRITES_TO_FD2 = "import os; os.write(2, b'CHILD_MARKER\\n')"


class TestTeeStderr:
    def test_captures_parent_process_writes(self):
        with tee_stderr() as cap:
            print("PARENT_MARKER", file=sys.stderr)
        assert "PARENT_MARKER" in cap.text()

    def test_output_still_reaches_real_stderr(self, capfd):
        """A tee that swallowed a 25-minute run's live output would be worse
        than no tee."""
        with tee_stderr() as cap:
            print("PASSTHROUGH_MARKER", file=sys.stderr)
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_stderr_capture.py -k Tee -v
```

Expected: FAIL with `ImportError: cannot import name 'tee_stderr'`.

- [ ] **Step 3: Implement the tee**

Append to `evals/at_scale/stderr_capture.py` (add `import contextlib`, `import os`, `import sys`, `import threading` to the imports):

```python
class _Capture:
    """Accumulates tee'd bytes. Held by the caller for the duration of the
    `with` block and read afterwards via text()."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


@contextlib.contextmanager
def tee_stderr():
    """Duplicate everything written to fd 2 into a buffer while still passing
    it through to the real stderr.

    Operates on the FILE DESCRIPTOR, not sys.stderr. _extract_commit runs in
    a ProcessPoolExecutor whose workers inherit fd 2 rather than the parent's
    sys.stderr object, and minigraf's error strings can reach fd 2 natively.
    A sys.stderr swap is blind to both -- see the ablation in
    tests/test_at_scale_stderr_capture.py, which fails loudly if that ceases
    to be true.
    """
    capture = _Capture()
    saved_fd = os.dup(2)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def pump() -> None:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            os.write(saved_fd, chunk)
            capture.append(chunk)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    try:
        yield capture
    finally:
        sys.stderr.flush()
        # Restoring fd 2 closes the pipe's write end, which is what gives the
        # pump thread its EOF. Order matters: join before closing read_fd.
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        pump_thread.join(timeout=10)
        os.close(read_fd)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_stderr_capture.py -v
```

Expected: all PASS. **If `test_ablation_...` fails on its first assertion**, the counterfactual does not hold — stop, report it, and replace `tee_stderr` with a `sys.stderr` swap rather than keeping unjustified complexity.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/stderr_capture.py tests/test_at_scale_stderr_capture.py
git commit -m "Add an fd-level stderr tee, with the ablation that justifies it

Capture is at the file descriptor, not sys.stderr, because _extract_commit's
ProcessPoolExecutor workers inherit fd 2 and minigraf's error strings can
reach it natively -- a sys.stderr swap is blind to both.

That is a counterfactual claim, so it carries an experiment: the ablation
asserts a sys.stderr swap MISSES child fd-2 output that the tee catches. If
that assertion ever fails, the fd-level machinery is unjustified and the
simpler swap should replace it.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 3: `--graph-path` persistence

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py:223-246` (the `main()` function)
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `resolve_graph_path(graph_path_arg: Optional[str])` — a context manager yielding a `Path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_ingestion_benchmark.py`:

```python
import pytest

from evals.at_scale.run_ingestion_benchmark import resolve_graph_path


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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py -k ResolveGraphPath -v
```

Expected: FAIL with `ImportError: cannot import name 'resolve_graph_path'`.

- [ ] **Step 3: Implement it**

In `evals/at_scale/run_ingestion_benchmark.py`, add `import contextlib` and `import tempfile` to the module-level imports (the current `main()` imports `tempfile` inline at line 235 — move it to the top and delete the inline import). Add above `main()`:

```python
@contextlib.contextmanager
def resolve_graph_path(graph_path_arg: Optional[str]):
    """Yield the graph path for one run.

    Without --graph-path this is a TemporaryDirectory, exactly as before
    (#120) -- the recurring benchmark's behaviour must not change. With it,
    the graph persists so a probe can query it after the run (#256); #251's
    occurrences were never inspectable precisely because the graph was
    already deleted.

    Refuses an existing path. run_ingestion_benchmark's own docstring states
    that precondition, and CLAUDE.md's standing rule is that graphs are
    rebuilt into a fresh path, never re-ingested in place -- re-running over
    an existing file repairs nothing and silently doubles the history.
    """
    if graph_path_arg is None:
        with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-") as tmpdir:
            yield Path(tmpdir) / "bench.graph"
        return

    path = Path(graph_path_arg)
    if path.exists():
        raise SystemExit(
            f"--graph-path {path} already exists. Each run needs a fresh "
            f"graph -- re-ingesting into an existing one is never correct. "
            f"Pick a new path or delete this one deliberately."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path
```

Then rewrite `main()`'s body to use it:

```python
    parser.add_argument(
        "--graph-path", default=None,
        help="Persist the graph at this path instead of using a temporary "
             "directory. Must not already exist. Required for the #256 "
             "provisional-residue probe, which queries the surviving graph.",
    )
    args = parser.parse_args()

    with resolve_graph_path(args.graph_path) as graph_path:
        metrics = asyncio.run(
            run_ingestion_benchmark(
                args.repo_path,
                args.branch,
                graph_path,
                poll_interval=args.poll_interval,
                duty_factor=args.poll_duty_factor,
                compare_ignore=args.compare_ignore,
            )
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py -v
```

Expected: all PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_ingestion_benchmark.py
git commit -m "Add --graph-path to the at-scale ingestion benchmark

#251's occurrences were never inspectable because the harness ingests into a
TemporaryDirectory -- the graph was gone before anyone could query it. This
makes persistence an option rather than a one-off script.

Refuses an existing path, enforcing the precondition
run_ingestion_benchmark's docstring already states and matching the standing
rule that graphs are rebuilt, never re-ingested in place. Omitting the flag
leaves today's behaviour untouched.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 4: Wire the tee and scanner into the run

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (`run_ingestion_benchmark` result dict at 172-188; `_exit_code` at 215-217)
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: `scan_ingestion_stderr` and `tee_stderr` from Task 1/2; `resolve_graph_path` from Task 3.
- Produces: metrics dict keys `skipped_commits`, `error_signals`, `correction_sweep_summaries`, `correction_sweep_skipped`; `_exit_code` honouring the first two.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_ingestion_benchmark.py`:

```python
from evals.at_scale.run_ingestion_benchmark import _exit_code


class TestExitCodeGate:
    def test_clean_run_exits_zero(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
        }) == 0

    def test_a_skipped_commit_fails_the_run(self):
        """processed and final_status are both blind to this -- the gate is
        the only thing that turns a dropped commit into a failure."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": ["abc123"],
            "error_signals": [],
        }) == 1

    def test_a_251_signature_fails_the_run(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [{"pattern": "page_out_of_bounds", "line": "..."}],
        }) == 1

    def test_error_status_still_fails(self):
        assert _exit_code({
            "final_status": "error",
            "skipped_commits": [],
            "error_signals": [],
        }) == 1

    def test_missing_keys_are_treated_as_clean_for_old_metrics(self):
        """Pre-#256 metrics files carry neither key; _exit_code must not
        crash reading them."""
        assert _exit_code({"final_status": "complete"}) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py -k ExitCodeGate -v
```

Expected: FAIL — `test_a_skipped_commit_fails_the_run` and `test_a_251_signature_fails_the_run` return 0.

- [ ] **Step 3: Implement**

In `evals/at_scale/run_ingestion_benchmark.py`, add the import:

```python
from evals.at_scale.stderr_capture import scan_ingestion_stderr, tee_stderr  # noqa: E402
```

Wrap the ingestion in the tee. In `run_ingestion_benchmark`, replace the `start = time.perf_counter()` through `wall_clock = ...` block so the tee spans the whole run, then scan after it closes:

```python
    start = time.perf_counter()
    with tee_stderr() as captured:
        ingest_task = asyncio.create_task(mcp_server._run_ingestion(repo_path, resolved_branch))
        try:
            status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
                ingest_task, poll_interval, duty_factor
            )
            await ingest_task
        except BaseException:
            if not ingest_task.done():
                ingest_task.cancel()
                try:
                    await ingest_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
    wall_clock = time.perf_counter() - start
    scanned = scan_ingestion_stderr(captured.text())
```

Then merge the scanned keys into the result dict, after `"checkpoint_summary": checkpoint_summary,`:

```python
        "checkpoint_summary": checkpoint_summary,
        # #256. Not derivable from commits_ingested or final_status: the
        # per-commit handler isolates failures and increments `processed`
        # anyway, so both are blind to a dropped commit.
        **scanned,
    }
```

Replace `_exit_code`:

```python
def _exit_code(metrics: dict[str, Any]) -> int:
    """Return 1 if ingestion ended in an error state, dropped any commit, or
    logged a #251 signature; else 0.

    The last two matter because `final_status` cannot see them --
    _run_ingestion isolates per-commit failures by design rather than
    propagating them, so a run that skipped commits still reports
    "complete". Keys are read with .get() so pre-#256 metrics files still
    evaluate.
    """
    if metrics.get("final_status") == "error":
        return 1
    if metrics.get("skipped_commits") or metrics.get("error_signals"):
        return 1
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py tests/test_at_scale_stderr_capture.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_ingestion_benchmark.py
git commit -m "Record dropped commits and #251 signatures in the run metrics

The at-scale run's only report of an isolated per-commit failure was a
stderr line nobody captured, so the #255 acceptance run's 'no errors' claim
rested on a human watching stdout. Tee the run's stderr, scan it, and put
the result in the metrics JSON where it can be checked.

_exit_code now fails on a dropped commit or a #251 signature. Neither is
reachable from final_status, which reports 'complete' for a run that skipped
commits.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 5: The probe's pure analysis primitives

Following `probe_dep_preload_exposure.py`'s convention: the analysis logic is importable and unit-tested without opening a graph; the headline number is what the probe discovers and is not asserted.

**Files:**
- Create: `evals/at_scale/probe_provisional_residue.py`
- Test: `tests/test_at_scale_provisional_residue_probe.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `read_sweep_total(metrics: dict) -> int`, `require_complete_run(metrics: dict) -> None`, `breakdown_by_entity_type(idents: Sequence[str]) -> dict[str, int]`, `residue_verdict(m: int, n: int) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_at_scale_provisional_residue_probe.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'evals.at_scale.probe_provisional_residue'`.

- [ ] **Step 3: Implement the primitives**

Create `evals/at_scale/probe_provisional_residue.py`:

```python
# evals/at_scale/probe_provisional_residue.py
"""#256: cross-check a surviving graph's provisional residue against the
correction sweep's own accounting.

Read-only, and a SEPARATE PROCESS by design. It opens the persisted graph
with no other handle live anywhere in the process -- querying in-process
while the benchmark's handle lifecycle unwinds is the exact hazard class
that produced #251/#253, and this probe exists to confirm that bug is gone.

The comparison is `M <= N`, not `M == N`:

  M = live :type/lineage-marker entities carrying :status :provisional
  N = the correction sweep's own "left provisional/unreconciled" total

N counts entities left provisional OR unreconciled. _correction_sweep_apply's
case 2 leaves an already-authoritative entity with an ambiguous
:introduced-by unreconciled without marking it provisional, so it raises N
without raising M. M is therefore a subset of N, and equality would fail on
a healthy graph -- as would asserting M == 0, since a non-empty residue is
the documented fail-safe, not a defect.

`M > N` means an entity sits provisional in the graph that the sweep never
accounted for: state left inconsistent by something other than the designed
fail-safe. That is the signature this probe detects.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def read_sweep_total(metrics: dict[str, Any]) -> int:
    """N, from the benchmark's metrics JSON.

    A missing key is fatal, not zero. Zero is a legitimate MEASURED value --
    _correction_sweep_log_summary prints only `if skipped_events:`, so an
    absent stderr line genuinely means no residue. A missing JSON key means
    something else entirely: metrics written before this instrumentation
    existed. Defaulting it to 0 would silently turn `M <= N` into `M == 0`
    and fail against a perfectly healthy graph.
    """
    if "correction_sweep_skipped" not in metrics:
        raise SystemExit(
            "metrics JSON has no 'correction_sweep_skipped' key -- it predates "
            "the #256 instrumentation. Re-run the benchmark; this probe will "
            "not guess N."
        )
    return int(metrics["correction_sweep_skipped"])


def require_complete_run(metrics: dict[str, Any]) -> None:
    """Refuse a graph whose run did not finish. Residue on an aborted run is
    not evidence of anything."""
    status = metrics.get("final_status")
    if status != "complete":
        raise SystemExit(
            f"run finished with final_status={status!r}, not 'complete' -- "
            f"provisional residue on an unfinished run means nothing."
        )


def breakdown_by_entity_type(idents: Sequence[str]) -> dict[str, int]:
    """Count provisional entities per entity-type namespace.

    The type is taken from the ident's own namespace (":function/foo" ->
    "function"), which is how idents are constructed, rather than a second
    query per entity.
    """
    return dict(Counter(ident.lstrip(":").split("/", 1)[0] for ident in idents))


def residue_verdict(m: int, n: int) -> dict[str, Any]:
    """Apply `M <= N` and keep both raw numbers.

    Both survive in the output on purpose: `M <= N` weakens as N grows -- if
    the sweep legitimately skips thousands, M could hide real corruption
    underneath -- so a future run must be able to compare N itself across
    runs. A jump in N is its own signal.
    """
    return {
        "provisional_entities": m,
        "sweep_skipped": n,
        "ok": m <= n,
        "interpretation": (
            "M <= N: provisional residue is within the correction sweep's own "
            "accounting."
            if m <= n
            else "M > N: provisional state the sweep never accounted for -- "
                 "the #251 signature."
        ),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_provisional_residue.py tests/test_at_scale_provisional_residue_probe.py
git commit -m "Add the #256 residue probe's analysis primitives

The comparison is M <= N, not M == N and not M == 0. A non-empty provisional
residue is _correction_sweep_apply's documented fail-safe, and N additionally
counts case-2 entities that are unreconciled without being provisional, so M
is a subset. Asserting equality or zero would fail on a healthy graph.

A missing correction_sweep_skipped key is fatal rather than defaulting to
zero: zero is a legitimate measured value, so defaulting would silently turn
M <= N into M == 0 and produce a false failure.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 6: The probe's graph query and CLI

**Files:**
- Modify: `evals/at_scale/probe_provisional_residue.py`
- Test: `tests/test_at_scale_provisional_residue_probe.py`

**Interfaces:**
- Consumes: `breakdown_by_entity_type`, `read_sweep_total`, `require_complete_run`, `residue_verdict` from Task 5.
- Produces: `provisional_entity_idents(db) -> list[str]`, `main() -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_provisional_residue_probe.py`. This uses a real graph — the repo's real-backend-only convention (`docs/testing-conventions.md`), and the same fixture shape the other at-scale probe tests use.

```python
from evals.at_scale.probe_provisional_residue import provisional_entity_idents


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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py -k ProvisionalEntityIdents -v
```

Expected: FAIL with `ImportError: cannot import name 'provisional_entity_idents'`.

- [ ] **Step 3: Implement the query and CLI**

Append to `evals/at_scale/probe_provisional_residue.py`:

```python
def provisional_entity_idents(db: Any) -> list[str]:
    """Every entity currently carrying a live provisional lineage marker.

    Mirrors _lineage_is_provisional's definition (mcp_server.py:5927) --
    a :type/lineage-marker companion entity exists for the entity -- but as
    one set-returning query instead of one existence check per entity.
    _lineage_confirm retracts the marker's facts wholesale, so "marker
    present" and "provisional" are the same predicate.
    """
    import mcp_server

    raw = mcp_server._db_execute(
        db,
        "(query [:find ?e :where "
        f"[?m :entity-type {mcp_server._LINEAGE_MARKER_ENTITY_TYPE}] "
        "[?m :status :provisional] "
        "[?m :entity ?e]])",
    )
    return sorted(row[0] for row in json.loads(raw).get("results", []))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-check a persisted graph's provisional residue "
                    "against the correction sweep's accounting (#256).",
    )
    parser.add_argument(
        "--graph-path", required=True,
        help="A graph produced by run_ingestion_benchmark.py --graph-path.",
    )
    parser.add_argument(
        "--metrics-json", required=True,
        help="That run's results/ingestion-<ts>.json, which carries N.",
    )
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics_json).read_text())
    require_complete_run(metrics)
    n = read_sweep_total(metrics)

    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(args.graph_path)
    try:
        with mcp_server.db_lease() as db:
            idents = provisional_entity_idents(db)
    finally:
        mcp_server._reset_db_state()

    result = residue_verdict(len(idents), n)
    # Record the inputs so the pairing of graph to metrics file is auditable
    # after the fact -- a probe run against the wrong graph or a stale JSON
    # is otherwise indistinguishable from a correct one.
    result["graph_path"] = str(Path(args.graph_path).resolve())
    result["metrics_json"] = str(Path(args.metrics_json).resolve())
    result["breakdown_by_entity_type"] = breakdown_by_entity_type(idents)
    result["correction_sweep_summaries"] = metrics.get("correction_sweep_summaries")

    out_path = REPO_ROOT / "evals" / "at_scale" / "results" / "256-provisional-residue.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")

    print(json.dumps(result, indent=2))
    print(f"\nWrote {out_path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py -v
```

Expected: all PASS.

- [ ] **Step 5: Run the full suite before the acceptance run**

```bash
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -20
```

Expected: no new failures relative to master. Record the pass/fail counts — do **not** report a collected total as a pass count.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_provisional_residue.py tests/test_at_scale_provisional_residue_probe.py
git commit -m "Add the #256 residue probe's graph query and CLI

Counts M in one set-returning query rather than one existence check per
entity, mirroring _lineage_is_provisional's definition -- _lineage_confirm
retracts the marker wholesale, so 'marker present' and 'provisional' are the
same predicate.

Tested against a graph with a KNOWN number of planted markers, including the
confirmed case, so a query that counts the wrong thing is visible rather
than merely plausible.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

---

### Task 7: The acceptance run

The only task whose output is an observation rather than an assertion. Takes ~25 minutes.

**Files:**
- Create: `evals/at_scale/results/ingestion-<ts>.json` (generated)
- Create: `evals/at_scale/results/256-provisional-residue.json` (generated)
- Modify: `evals/at_scale/benchmark.md` (appended by the harness)

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: the committed evidence for #256.

- [ ] **Step 1: Run the benchmark against a persistent graph**

The graph goes to the scratchpad, **not** the repo — it is ~211 MB plus an ~89 MB index, and `results/` is committed.

```bash
GRAPH=/tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/9d9c2dc5-7aff-4d0e-8820-51ec2500f443/scratchpad/256-acceptance.graph
LOG=/tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/9d9c2dc5-7aff-4d0e-8820-51ec2500f443/scratchpad/256-run.log
.venv/bin/python -m evals.at_scale.run_ingestion_benchmark \
    --repo-path . --graph-path "$GRAPH" 2>&1 | tee "$LOG"
# PIPESTATUS[0], not $? -- $? here is tee's status, which is always 0 and
# would mask the very failure gate this run exists to trip.
echo "exit=${PIPESTATUS[0]}"
```

Expected: ~25 minutes, exit 0. A non-zero exit means a commit was dropped or a #251 signature appeared — that is a **finding**, not a step to retry. Stop and report it.

- [ ] **Step 2: Confirm the metrics carry the new keys**

```bash
.venv/bin/python -c "
import json, glob, os
p = max(glob.glob('evals/at_scale/results/ingestion-*.json'), key=os.path.getmtime)
d = json.load(open(p))
print('file:', p)
for k in ('final_status','commits_ingested','skipped_commits','error_signals','correction_sweep_skipped','correction_sweep_summaries'):
    print(f'{k} = {d.get(k)!r}')
"
```

Expected: `final_status='complete'`, `skipped_commits=[]`, `error_signals=[]`, and `correction_sweep_skipped` present as an integer (0 is valid).

- [ ] **Step 3: Run the probe against the surviving graph**

```bash
.venv/bin/python -m evals.at_scale.probe_provisional_residue \
    --graph-path "$GRAPH" \
    --metrics-json "$(ls -t evals/at_scale/results/ingestion-*.json | head -1)"
echo "exit=$?"
```

Expected: exit 0 with `"ok": true`. Exit 1 (`M > N`) is a **finding** — provisional state the sweep never accounted for. Stop and report it rather than adjusting the threshold.

- [ ] **Step 4: Verify the graph is not staged**

```bash
git status --short
```

Expected: only `evals/at_scale/benchmark.md` and the two `results/*.json` files. If the graph or its `.fts.sqlite3` index appears, the `$GRAPH` path was wrong — do not commit it.

- [ ] **Step 5: Commit the evidence**

Fill the real measured numbers into the message; do not copy the placeholders below verbatim.

```bash
git add evals/at_scale/benchmark.md evals/at_scale/results/
git commit -m "Record the #256 persistent-graph acceptance run

The confirmation #251 asked for and could never perform: its occurrences were
never inspectable because the harness deleted the graph before anyone could
query it.

<N_COMMITS> commits, final_status complete, <WALL>s. Zero dropped commits and
zero #251 signatures -- both now captured from a tee'd stderr rather than
resting on a human having watched stdout, as the #255 run's claim did.

Provisional residue M=<M> against the correction sweep's own N=<N>, so
M <= N holds: every entity left provisional is within the sweep's documented
fail-safe, with none unaccounted for. Both raw numbers are recorded because
M <= N weakens as N grows -- a future run should compare N itself.

The graph is an intermediate, not an artifact, and is not committed.

Refs #256"
git log -1 --format=%B | grep -inE "close[sd]?|fix(e[sd])?|resolve[sd]?" || echo "clean"
```

- [ ] **Step 6: Delete the scratch graph**

```bash
rm -f "$GRAPH" "$GRAPH".wal "$GRAPH".fts.sqlite3 "$GRAPH".lock
```

---

## Acceptance

#256 is satisfied when all of these hold:

1. A persistent-graph at-scale run over master's full history completes with `final_status: complete`, empty `skipped_commits`, and empty `error_signals`.
2. The probe reports `M <= N` against that run's surviving graph, with M, N, and the per-type breakdown recorded.
3. Both JSON results and the `benchmark.md` entry are committed; the graph is not.
4. The full test suite is green.

## Out of scope

- **#272** (instance-level monkeypatch of `_lease_manager`). Nothing here touches `_lease_manager`; #272 stays open for its own branch.
- **Git-verifying a sample of the provisional entities** against real history to confirm each is genuinely ambiguous. Worth its own issue if `M > N` ever trips; building it now would gold-plate a verification run.
- **Any `mcp_server.py` change.** If one seems necessary, re-read the spec first.
