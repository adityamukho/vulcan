# `:depends-on` Preload Exposure Probe + Benchmark Poller Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quantify #245's `:depends-on` preload exposure against real history, and fix the #242 benchmark poller that currently makes a full-history ingestion unfinishable.

**Architecture:** Two independent changes on one branch. #242 moves the benchmark's in-flight poll off the event loop and gives it an adaptive interval, bounding its share of `_db_native_lock`. #245 gets a new read-only probe that ingests this repo into a scratch graph, then drives the *real* `_preload_known_entities` + `_preload_known_deps` at each structurally-affected watermark position and diffs their output against a position-exact offline oracle.

**Tech Stack:** Python 3.10+, pytest + pytest-asyncio, `mcp_server.py`'s in-process handlers, `frontier_registry.build_linearization`, minigraf `:any-valid-time` queries.

**Spec:** `docs/superpowers/specs/2026-08-07-dep-preload-exposure-probe-design.md`

## Global Constraints

- **This branch fixes nothing in `:depends-on` / `:pinned-commit` handling.** No change to `_preload_known_deps`, `_preload_pinned_commits`, or the fact model. #245's three options stay unbuilt until the measurement says which, if any, is warranted.
- **Issue-keyword discipline.** Commit messages and the PR body use `Refs #245`, `Refs #238`, `Refs #222`. Only `Closes #242` is a closing keyword. GitHub scans both commit messages and the PR body, and on this project a *negated* "does not close #N" has still auto-closed an issue — so never write a closing keyword next to #238 or #245 in any form.
- **Branch:** `probe-245-dep-preload-exposure`, already created, spec already committed at `0ede760`.
- **`mcp` stays capped `<2.0.0`.** Do not touch the dependency pin.
- **Ablation is mandatory for the #242 regression tests.** A test that passes against the pre-fix code is not testing anything; see Task 1 Step 2.
- **Timestamps are second-granularity.** `_git_commits` formats `"%Y-%m-%dT%H:%M:%SZ"` (`mcp_server.py:4133`), so distinct commits can share a timestamp. Every task that inverts a timestamp to a position must handle a list of positions, never a single one.
- **Existing test to update:** `tests/test_at_scale_ingestion_benchmark.py:42-46` asserts an exact metric key set. Task 2 adds keys and must update it.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `evals/at_scale/run_ingestion_benchmark.py` | Modify | #242: off-loop adaptive poller, new poll metrics |
| `evals/at_scale/report.py` | Modify | Render the poll-duty row in the benchmark report |
| `evals/at_scale/benchmark.md` | Modify | Dated note that prior entries carry poller overhead |
| `evals/at_scale/probe_dep_preload_exposure.py` | Create | #245 probe: pure analysis functions + driver |
| `tests/test_at_scale_ingestion_benchmark.py` | Modify | #242 ablation-proven poller tests; key-set update |
| `tests/test_at_scale_dep_preload_probe.py` | Create | Probe oracle + affected-position unit tests |

---

### Task 1: Move the benchmark poll off the event loop and bound its duty cycle

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py:12-51`
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_poll_during_ingestion(ingest_task, poll_interval, duty_factor=10.0) -> tuple[list[float], list[float], list[float]]` returning `(status_latencies, query_latencies, poll_offsets)`. Note this is a **3-tuple**; it was a 2-tuple. Task 2 consumes all three.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_ingestion_benchmark.py`:

```python
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
```

- [ ] **Step 2: Run the tests against the CURRENT implementation and confirm they fail**

Run: `pytest tests/test_at_scale_ingestion_benchmark.py::TestPollerDoesNotStarveTheEventLoop -v`

Expected: all three FAIL. The first two fail on the assertion (starved loop / no backoff); the third fails on `TypeError` from unpacking a 2-tuple into three names, and on the unexpected `duty_factor` keyword.

**This is the ablation and it is mandatory.** If the first two tests *pass* against the current code, they are not exercising #242's mechanism — stop, report that plainly, and rework them before writing any implementation. Do not proceed on a green ablation.

- [ ] **Step 3: Add the import**

In `evals/at_scale/run_ingestion_benchmark.py`, add to the stdlib import block (after `import asyncio`, keeping alphabetical order):

```python
import concurrent.futures
```

- [ ] **Step 4: Replace `_poll_during_ingestion`**

Replace lines 31-51 entirely:

```python
async def _poll_during_ingestion(
    ingest_task: "asyncio.Task[None]",
    poll_interval: float,
    duty_factor: float = 10.0,
) -> tuple[list[float], list[float], list[float]]:
    """Poll ingest_status and a graph query while ingest_task runs.

    Returns (status_latencies, query_latencies, poll_offsets); latencies in
    seconds, offsets in seconds since polling began.

    #242: both halves of this are load-bearing.

    The handlers run on a dedicated single-worker executor rather than inline,
    because handle_minigraf_query is synchronous -- calling it on the event
    loop stalls the _run_ingestion coroutine, the process-pool result
    collection, and every write_executor completion. Two full-history runs
    were killed at 3h54m and 36m on code measured at +3% before this was
    understood.

    Off-loop alone is NOT sufficient. handle_minigraf_query acquires
    _db_native_lock (mcp_server._db_execute), and _STATUS_QUERY counts every
    :type/commit entity, so its cost grows for the whole run -- a ~1s scan
    every 0.5s would still serialize against every ingestion write from
    another thread. The adaptive interval is what bounds the instrument's
    share of that lock: at duty_factor=10 the poller holds it for at most
    ~9% of the run however large the scan grows.

    _STATUS_QUERY is deliberately unchanged, so the recorded latency series
    stays comparable to the entries already in benchmark.md.

    A dedicated executor, rather than the loop default, keeps the poll off any
    thread the ingestion may want.

    Known limitation: a cancelled run still waits on an in-flight poll thread
    at executor shutdown. The previous implementation blocked the event loop
    outright, so this is strictly better, but it is not zero.
    """
    import mcp_server

    loop = asyncio.get_running_loop()
    status_latencies: list[float] = []
    query_latencies: list[float] = []
    poll_offsets: list[float] = []
    started = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="bench-poll"
    ) as poll_executor:
        while not ingest_task.done():
            poll_offsets.append(time.perf_counter() - started)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_ingest_status
            )
            status_duration = time.perf_counter() - t0
            status_latencies.append(status_duration)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_query, _STATUS_QUERY
            )
            query_duration = time.perf_counter() - t0
            query_latencies.append(query_duration)

            await asyncio.sleep(
                max(poll_interval, duty_factor * (status_duration + query_duration))
            )

    return status_latencies, query_latencies, poll_offsets
```

- [ ] **Step 5: Update the single existing call site**

`evals/at_scale/run_ingestion_benchmark.py:83` currently unpacks two values. Change it to three:

```python
        status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
            ingest_task, poll_interval
        )
```

- [ ] **Step 6: Run the new tests and confirm they pass**

Run: `pytest tests/test_at_scale_ingestion_benchmark.py::TestPollerDoesNotStarveTheEventLoop -v`
Expected: 3 passed.

- [ ] **Step 7: Run the whole benchmark test module for regressions**

Run: `pytest tests/test_at_scale_ingestion_benchmark.py -v`
Expected: `TestPollerDoesNotStarveTheEventLoop` passes; `test_returns_expected_metric_keys` still passes (Task 1 adds no metric keys); everything else passes.

- [ ] **Step 8: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_ingestion_benchmark.py
git commit -m "$(cat <<'EOF'
Run the benchmark poll off the event loop with a bounded duty cycle

The poll ran handle_minigraf_query synchronously on the event loop, stalling
the _run_ingestion coroutine, process-pool result collection, and every
write_executor completion. _STATUS_QUERY's cost grows all run, so the
instrument's share of the loop rose monotonically until ingestion approached
a standstill.

Off-loop alone is not sufficient: handle_minigraf_query takes
_db_native_lock, so a growing scan would still serialize against every
ingestion write from another thread. The adaptive interval bounds that share
at ~9% for the default duty_factor=10.

_STATUS_QUERY is unchanged so the latency series stays comparable to the
entries already in benchmark.md.

Tests are ablation-proven: both regression tests were run against the
pre-fix implementation and fail there.

Closes #242

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01T8qWT72sj5ja3wvkcgkK6b
EOF
)"
```

---

### Task 2: Surface poll duty cycle in the benchmark result and report

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (result dict, `main`'s argparse)
- Modify: `evals/at_scale/report.py:31-56`
- Modify: `evals/at_scale/benchmark.md`
- Test: `tests/test_at_scale_ingestion_benchmark.py:42-46`

**Interfaces:**
- Consumes: `_poll_during_ingestion(...) -> (status_latencies, query_latencies, poll_offsets)` from Task 1.
- Produces: result-dict keys `poll_count: int`, `poll_duty_fraction: float`, `poll_offsets: list[float]`.

`poll_duty_fraction` is the acceptance criterion for #242 — it is the direct evidence that the instrument is no longer creating the load it measures.

- [ ] **Step 1: Write the failing test**

Replace the body of `test_returns_expected_metric_keys` (`tests/test_at_scale_ingestion_benchmark.py:39-46`) with:

```python
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
```

And add to `TestRunIngestionBenchmark`:

```python
    @pytest.mark.asyncio
    async def test_poll_duty_fraction_is_a_bounded_fraction(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["poll_count"] == len(metrics["poll_offsets"])
        assert 0.0 <= metrics["poll_duty_fraction"] <= 1.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_at_scale_ingestion_benchmark.py::TestRunIngestionBenchmark -v`
Expected: `test_returns_expected_metric_keys` and `test_poll_duty_fraction_is_a_bounded_fraction` FAIL with a `KeyError`/set-inequality on the three new keys.

- [ ] **Step 3: Add the metrics to the result dict**

In `run_ingestion_benchmark`, immediately after `wall_clock = time.perf_counter() - start` (line 93), add:

```python
    poll_seconds = sum(status_latencies) + sum(query_latencies)
    poll_duty_fraction = (poll_seconds / wall_clock) if wall_clock > 0 else 0.0
```

Then add three entries to the `result` dict, after `"query_latency": ...`:

```python
        "poll_count": len(poll_offsets),
        "poll_duty_fraction": poll_duty_fraction,
        "poll_offsets": poll_offsets,
```

- [ ] **Step 4: Plumb `--poll-duty-factor` through `main` and `run_ingestion_benchmark`**

Add a parameter to `run_ingestion_benchmark`'s signature, after `poll_interval`:

```python
    duty_factor: float = 10.0,
```

Pass it at the call site from Task 1 Step 5:

```python
        status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
            ingest_task, poll_interval, duty_factor
        )
```

In `main`, add the argument after `--poll-interval`:

```python
    parser.add_argument(
        "--poll-duty-factor", type=float, default=10.0,
        help="Sleep max(poll_interval, N * last_poll_duration) between polls, "
             "bounding the instrument's share of _db_native_lock (#242).",
    )
```

`main` currently passes positionally (`args.poll_interval, args.compare_ignore`). Switch to keywords so the new parameter cannot be mis-bound to `compare_ignore`:

```python
            run_ingestion_benchmark(
                args.repo_path,
                args.branch,
                graph_path,
                poll_interval=args.poll_interval,
                duty_factor=args.poll_duty_factor,
                compare_ignore=args.compare_ignore,
            )
```

- [ ] **Step 5: Render the duty cycle in the report**

In `evals/at_scale/report.py`, inside `append_ingestion_report`'s `lines` list, add after the graph-query-latency row (line 55):

```python
        f"| Poll duty cycle (#242) | {metrics['poll_duty_fraction']*100:.2f}% "
        f"over {metrics['poll_count']} polls |",
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_at_scale_ingestion_benchmark.py tests/test_at_scale_report.py -v`
Expected: all pass.

If `tests/test_at_scale_report.py` builds a metrics dict literal, it needs `poll_duty_fraction` and `poll_count` added to that fixture — fix it there, do not make the report row conditional. The row is the acceptance criterion and must always render.

- [ ] **Step 7: Add the `benchmark.md` note**

Insert immediately after the existing header block at the top of `evals/at_scale/benchmark.md`, before the first run section:

```markdown
> **2026-08-07 — poller overhead in entries before this date (#242).** Every
> ingestion entry recorded before 2026-08-07 was measured by a harness whose
> in-flight poller ran a blocking, cost-growing graph query on the event loop
> every 0.5s, starving the ingestion it measured. Entry-to-entry comparisons
> remain valid — both sides carry the same instrument — but the absolute
> wall-clock figures, including the 78.87s forward-only baseline and the
> 1,600.55s post-#236 figure, overstate real ingestion cost by an unquantified
> margin. Entries from 2026-08-07 onward carry a "Poll duty cycle" row; treat
> its absence as "unmeasured, assume inflated".
```

- [ ] **Step 8: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py evals/at_scale/report.py \
        evals/at_scale/benchmark.md tests/test_at_scale_ingestion_benchmark.py \
        tests/test_at_scale_report.py
git commit -m "$(cat <<'EOF'
Record the benchmark poller's duty cycle, and flag pre-fix entries

poll_duty_fraction is the acceptance criterion for the poller fix: it is
direct evidence the instrument is no longer creating the load it measures.
poll_offsets are recorded because the adaptive interval makes the latency
sample irregular, so percentiles are no longer over a uniform series.

benchmark.md gains a dated note that every entry before today carries poller
overhead -- entry-to-entry comparisons stay valid, absolute figures overstate
real cost.

Refs #242

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01T8qWT72sj5ja3wvkcgkK6b
EOF
)"
```

---

### Task 3: Probe analysis primitives — affected positions and the position oracle

**Files:**
- Create: `evals/at_scale/probe_dep_preload_exposure.py`
- Test: `tests/test_at_scale_dep_preload_probe.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all pure and importable by Task 4:
  - `build_ts_positions(commit_metadata: list[tuple]) -> dict[str, list[int]]`
  - `affected_positions(commit_metadata: list[tuple]) -> list[int]`
  - `resume_envelopes(commit_metadata: list[tuple]) -> list[str]` — `T_hi(W)` for every `W`
  - `invert_ms_to_positions(ms: int, ts_positions: dict[str, list[int]]) -> list[int]`
  - `edge_live_at(vf_positions: list[int], vt_positions: list[int] | None, w: int) -> bool`
  - `VALID_TIME_FOREVER_MS: int`

`commit_metadata` is `_git_commits`' return shape: `(hash, ts_iso, author_email, subject)`, ISO at **second** granularity.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_at_scale_dep_preload_probe.py`:

```python
# tests/test_at_scale_dep_preload_probe.py
"""Unit tests for the #245 exposure probe's analysis primitives.

The probe's headline number cannot be asserted -- it is what the probe exists
to discover. What CAN be asserted are the two components that could silently
produce a WRONG number: the timestamp-to-position inversion and the
affected-position derivation. That is the exact error class that cost two fix
rounds on #238, where a date-bound counterfactual made an inadequate test look
adequate.
"""

from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    affected_positions,
    build_ts_positions,
    edge_live_at,
    invert_ms_to_positions,
    resume_envelopes,
)

# Position 2 is inverted: it sits above position 1 but carries an earlier date.
# Positions 3 and 4 share a timestamp, so inversion of that instant is
# ambiguous and must yield both.
META = [
    ("h0", "2026-01-01T00:00:00Z", "a@b.com", "s0"),
    ("h1", "2026-01-05T00:00:00Z", "a@b.com", "s1"),
    ("h2", "2026-01-02T00:00:00Z", "a@b.com", "s2"),
    ("h3", "2026-01-06T00:00:00Z", "a@b.com", "s3"),
    ("h4", "2026-01-06T00:00:00Z", "a@b.com", "s4"),
]


class TestBuildTsPositions:
    def test_maps_each_timestamp_to_its_positions(self):
        assert build_ts_positions(META)["2026-01-01T00:00:00Z"] == [0]

    def test_collision_yields_every_colliding_position(self):
        assert build_ts_positions(META)["2026-01-06T00:00:00Z"] == [3, 4]


class TestResumeEnvelopes:
    def test_envelope_is_the_running_maximum(self):
        assert resume_envelopes(META) == [
            "2026-01-01T00:00:00Z",
            "2026-01-05T00:00:00Z",
            "2026-01-05T00:00:00Z",
            "2026-01-06T00:00:00Z",
            "2026-01-06T00:00:00Z",
        ]


class TestAffectedPositions:
    def test_selects_exactly_the_structurally_exposed_positions(self):
        # W=0: T_hi == ts, and no later position carries a date <= 01-01. Clean.
        # W=1: position 2 is above it with an earlier date -> wrong inclusion.
        # W=2: T_hi (01-05) > ts (01-02)             -> wrong exclusion.
        # W=3: position 4 is above it and ties its date -> wrong inclusion,
        #      because the bound is half-open containment [vf, vt) ∋ ts(W)
        #      and vf <= ts(W) admits an exact tie.
        # W=4: last position; T_hi == ts and nothing above. Clean.
        assert affected_positions(META) == [1, 2, 3]

    def test_a_strictly_monotonic_history_exposes_nothing(self):
        monotonic = [
            ("h0", "2026-01-01T00:00:00Z", "a@b.com", "s0"),
            ("h1", "2026-01-02T00:00:00Z", "a@b.com", "s1"),
            ("h2", "2026-01-03T00:00:00Z", "a@b.com", "s2"),
        ]
        assert affected_positions(monotonic) == []

    def test_empty_history_is_handled(self):
        assert affected_positions([]) == []


class TestInvertMsToPositions:
    def test_inverts_a_unique_timestamp(self):
        ts_positions = build_ts_positions(META)
        # 2026-01-02T00:00:00Z
        assert invert_ms_to_positions(1767312000000, ts_positions) == [2]

    def test_inverts_a_collided_timestamp_to_both_positions(self):
        ts_positions = build_ts_positions(META)
        # 2026-01-06T00:00:00Z
        assert invert_ms_to_positions(1767657600000, ts_positions) == [3, 4]

    def test_unknown_timestamp_yields_no_positions(self):
        assert invert_ms_to_positions(1, build_ts_positions(META)) == []


class TestEdgeLiveAt:
    def test_open_edge_is_live_at_and_after_its_introduction(self):
        assert edge_live_at([1], None, 1) is True
        assert edge_live_at([1], None, 4) is True

    def test_open_edge_is_not_live_below_its_introduction(self):
        assert edge_live_at([1], None, 0) is False

    def test_closed_edge_is_not_live_at_or_after_its_close(self):
        assert edge_live_at([1], [3], 3) is False
        assert edge_live_at([1], [3], 2) is True

    def test_ambiguous_introduction_uses_the_earliest_colliding_position(self):
        # A collided vf could be either position; the earliest is the only
        # choice that cannot understate exposure.
        assert edge_live_at([3, 4], None, 3) is True

    def test_ambiguous_close_uses_the_latest_colliding_position(self):
        # Symmetrically, the latest close cannot understate exposure.
        assert edge_live_at([1], [3, 4], 3) is True
        assert edge_live_at([1], [3, 4], 4) is False

    def test_unmappable_introduction_is_not_live_anywhere(self):
        assert edge_live_at([], None, 0) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_at_scale_dep_preload_probe.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'evals.at_scale.probe_dep_preload_exposure'`.

- [ ] **Step 3: Create the module with the analysis primitives**

Create `evals/at_scale/probe_dep_preload_exposure.py`:

```python
# evals/at_scale/probe_dep_preload_exposure.py
"""#245 exposure probe: how much does the :depends-on preload's ts(W) bound
actually misclassify against real history?

#238 replaced the forward-walk entity preload's author-date bound with a
position-indexed one. That fix reached three of four preload sites.
_preload_known_deps and _preload_pinned_commits stayed at ts(W) -- the
watermark commit's own author date -- because those facts carry no commit
reference to join a :hash to, and so admit no position clause.

This probe MEASURES that residual. It fixes nothing, and the oracle below is
NOT a candidate fix -- see position_exact_live_edges' docstring.
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

# minigraf's i64::MAX "still open" :db/valid-to sentinel. Mirrors
# mcp_server._VALID_TIME_FOREVER_MS (mcp_server.py:7450) exactly; duplicated
# rather than imported so the analysis primitives stay importable without
# opening a graph. If the two ever diverge, every open edge is misread as
# closed at a nonsense position, so keep them identical.
VALID_TIME_FOREVER_MS = (1 << 63) - 1

CommitMeta = Tuple[str, str, str, str]  # (hash, ts_iso, author_email, subject)


def build_ts_positions(commit_metadata: Sequence[CommitMeta]) -> Dict[str, List[int]]:
    """Map each author-date timestamp to every linearization position holding it.

    A LIST, not a single position, and deliberately so: _git_commits formats
    "%Y-%m-%dT%H:%M:%SZ" (second granularity), so distinct commits routinely
    share an instant. Collapsing that to one position would silently pick a
    winner and produce a confidently wrong exposure number.
    """
    ts_positions: Dict[str, List[int]] = {}
    for pos, (_hash, ts_iso, _author, _subject) in enumerate(commit_metadata):
        ts_positions.setdefault(ts_iso, []).append(pos)
    return ts_positions


def resume_envelopes(commit_metadata: Sequence[CommitMeta]) -> List[str]:
    """T_hi(W) = max(ts[0..W]) for every W, the bound _preload_known_entities
    takes after #238.

    Timestamps are fixed-width UTC, so lexicographic max is chronological max
    -- the same property mcp_server._resume_envelope relies on.
    """
    envelopes: List[str] = []
    running_max = ""
    for _hash, ts_iso, _author, _subject in commit_metadata:
        running_max = max(running_max, ts_iso)
        envelopes.append(running_max)
    return envelopes


def affected_positions(commit_metadata: Sequence[CommitMeta]) -> List[int]:
    """Positions where the ts(W) bound is structurally capable of misclassifying.

    This is a position-level precondition computed from commit_metadata alone,
    independent of any fact. It is what keeps the sweep off every position in
    the history.

    W is exposed iff either direction is possible there:

      wrong exclusion -- T_hi(W) > ts(W): some commit at position <= W carries
      a LATER date. A fact introduced there is live at W but falls outside the
      ts(W) bound, so the resuming walk cannot see it.

      wrong inclusion -- min(ts[W+1..]) <= ts(W): some commit ABOVE W carries a
      date at or below W's own. A fact introduced there is not yet live at W
      but falls inside the bound, so the resuming walk sees a future edge.

    The wrong-inclusion test uses <=, not <, because the bound is half-open
    containment [vf, vt) ∋ ts(W) (mcp_server._valid_time_window_clauses), whose
    vf test is `<=` -- a fact starting exactly at the instant is live.
    """
    timestamps = [ts for _h, ts, _a, _s in commit_metadata]
    n = len(timestamps)
    if n == 0:
        return []

    envelopes = resume_envelopes(commit_metadata)

    # suffix_min[i] = min(timestamps[i+1 .. n-1]), or None past the end.
    suffix_min: List[Optional[str]] = [None] * n
    running_min: Optional[str] = None
    for i in range(n - 1, -1, -1):
        suffix_min[i] = running_min
        running_min = timestamps[i] if running_min is None else min(running_min, timestamps[i])

    affected: List[int] = []
    for w in range(n):
        wrong_exclusion = envelopes[w] > timestamps[w]
        wrong_inclusion = suffix_min[w] is not None and suffix_min[w] <= timestamps[w]
        if wrong_exclusion or wrong_inclusion:
            affected.append(w)
    return affected


def invert_ms_to_positions(ms: int, ts_positions: Dict[str, List[int]]) -> List[int]:
    """Map an epoch-millisecond :db/valid-from or :db/valid-to back to the
    linearization positions whose commit carries that instant.

    Returns [] when no commit matches. The caller MUST treat that as a
    diagnostic, not as an empty result to skip: an unmappable fact means the
    inversion assumption is broken, which invalidates the measurement rather
    than shrinking it.
    """
    ts_iso = (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return list(ts_positions.get(ts_iso, []))


def edge_live_at(
    vf_positions: List[int],
    vt_positions: Optional[List[int]],
    w: int,
) -> bool:
    """Is a fact introduced at vf_positions and closed at vt_positions live at
    position w?

    vt_positions is None for a fact still open (the forever sentinel).

    Ambiguity resolution is deliberately asymmetric, and always in the
    direction that cannot UNDERSTATE exposure: an ambiguous introduction takes
    the earliest colliding position, an ambiguous close the latest. Understating
    is the dangerous direction here -- it would argue for closing #245 as
    negligible on a number that was rounded in our own favour.

    An empty vf_positions (unmappable introduction) is never live; the driver
    counts these separately.
    """
    if not vf_positions:
        return False
    introduced_at = min(vf_positions)
    if introduced_at > w:
        return False
    if vt_positions:
        return w < max(vt_positions)
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_at_scale_dep_preload_probe.py -v`
Expected: all pass.

If `test_inverts_a_unique_timestamp` fails on the literal epoch value, print the actual value and correct the literal in the test — the ISO strings in `META` are the authority, not the hand-computed milliseconds.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_dep_preload_exposure.py tests/test_at_scale_dep_preload_probe.py
git commit -m "$(cat <<'EOF'
Add the #245 probe's affected-position and inversion primitives

affected_positions computes a fact-independent precondition -- T_hi(W) > ts(W)
for wrong exclusion, min(ts[W+1..]) <= ts(W) for wrong inclusion -- so the
sweep visits only the positions where the ts(W) bound can misclassify at all.

The timestamp inversion returns a LIST of positions. _git_commits formats to
second granularity, so distinct commits routinely share an instant; collapsing
that would silently pick a winner and produce a confidently wrong number.
Ambiguity is resolved asymmetrically, always away from understating exposure.

Measures only. Refs #245, refs #238.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01T8qWT72sj5ja3wvkcgkK6b
EOF
)"
```

---

### Task 4: Probe driver — ingest, sweep, diff, report

**Files:**
- Modify: `evals/at_scale/probe_dep_preload_exposure.py`
- Test: `tests/test_at_scale_dep_preload_probe.py`

**Interfaces:**
- Consumes: everything Task 3 produced.
- Produces:
  - `gitlink_event_count(repo_path: str) -> int`
  - `load_dep_edges(db) -> list[dict]` — each `{"src": str, "dep": str, "vf_ms": int, "vt_ms": int}`
  - `position_exact_live_edges(edges, ts_positions, file_entities, w) -> set[tuple[str, str]]`
  - `sweep(db, repo_path, branch, linearization, commit_metadata) -> dict` — the full report
  - `main() -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_dep_preload_probe.py`:

```python
import subprocess as _subprocess

import pytest

from evals.at_scale.probe_dep_preload_exposure import (
    gitlink_event_count,
    position_exact_live_edges,
)


class TestPositionExactLiveEdges:
    """The oracle restricts to edges whose SOURCE MODULE is present in
    file_entities at W, mirroring _preload_known_deps' own ident_to_file
    filter (mcp_server.py:7526-7535, 7558-7560). That narrowing is already
    position-correct after #238, so isolating it out leaves the :depends-on
    bound as the only variable under measurement -- which is exactly #245's
    residual class."""

    def _edges(self):
        return [
            # live from position 1 onward, source module present
            {"src": ":module/a-py", "dep": ":module/b-py", "vf_ms": 1767225600000, "vt_ms": (1 << 63) - 1},
            # source module absent from file_entities -- must be excluded
            {"src": ":module/gone-py", "dep": ":module/b-py", "vf_ms": 1767225600000, "vt_ms": (1 << 63) - 1},
        ]

    def test_excludes_edges_whose_source_module_is_not_a_live_file_entity(self):
        ts_positions = {"2026-01-01T00:00:00Z": [1]}
        live = position_exact_live_edges(
            self._edges(), ts_positions, file_entities={"a.py": []}, w=2
        )
        assert live == {(":module/a-py", ":module/b-py")}

    def test_excludes_edges_introduced_above_the_position(self):
        ts_positions = {"2026-01-01T00:00:00Z": [1]}
        live = position_exact_live_edges(
            self._edges(), ts_positions, file_entities={"a.py": []}, w=0
        )
        assert live == set()


@pytest.fixture
def repo_with_submodule(tmp_path):
    """A repo carrying a real gitlink entry, so gitlink_event_count has
    something to find."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _subprocess.run(["git", "init"], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=inner, check=True, capture_output=True)
    (inner / "x.py").write_text("x = 1\n")
    _subprocess.run(["git", "add", "."], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "inner"], cwd=inner, check=True, capture_output=True)

    outer = tmp_path / "outer"
    outer.mkdir()
    _subprocess.run(["git", "init"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=outer, check=True, capture_output=True)
    (outer / "main.py").write_text("y = 2\n")
    _subprocess.run(["git", "add", "."], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "outer"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub"],
        cwd=outer, check=True, capture_output=True,
    )
    _subprocess.run(["git", "commit", "-m", "add sub"], cwd=outer, check=True, capture_output=True)
    return outer


class TestGitlinkEventCount:
    def test_counts_zero_for_a_repo_without_submodules(self, git_repo):
        assert gitlink_event_count(str(git_repo)) == 0

    def test_counts_a_real_gitlink_event(self, repo_with_submodule):
        assert gitlink_event_count(str(repo_with_submodule)) >= 1
```

Add the `git_repo` fixture to this module too — copy it verbatim from `tests/test_at_scale_ingestion_benchmark.py:20-34`. Do not import it across test modules; pytest fixtures are module-scoped here and the existing file does not export a conftest fixture.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_at_scale_dep_preload_probe.py -v`
Expected: `ImportError` for `gitlink_event_count` and `position_exact_live_edges`.

- [ ] **Step 3: Add the gitlink check and the oracle**

Append to `evals/at_scale/probe_dep_preload_exposure.py`:

```python
def gitlink_event_count(repo_path: str) -> int:
    """Number of raw diff entries across all history touching a gitlink
    (mode 160000).

    :pinned-commit facts are written only by gitlink handling, so a zero here
    means this history produces none and its #245 exposure is structurally
    unmeasurable -- not zero-risk, unmeasurable. That distinction goes in the
    report verbatim.
    """
    import subprocess

    result = subprocess.run(
        ["git", "log", "--all", "--raw", "--no-abbrev", "--format=%H"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return sum(
        1 for line in result.stdout.splitlines()
        if line.startswith(":") and "160000" in line
    )


def position_exact_live_edges(
    edges: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
    file_entities: Dict[str, List[str]],
    w: int,
) -> set:
    """The (src_ident, dep_ident) edges genuinely live at position w.

    NOT A CANDIDATE FIX, and must never be read as one. This works only
    because the entire history is in hand at analysis time: it inverts each
    fact's stored timestamps back to positions. A resuming forward walk has no
    such thing -- that is the whole reason #245 exists. It is a measurement
    device and nothing else. None of #245's three options resemble it.

    Restricted to edges whose source module appears in file_entities, mirroring
    _preload_known_deps' own ident_to_file filter. That narrowing is already
    position-correct after #238, so holding it fixed leaves the ts(W)
    :depends-on bound as the single variable under measurement.
    """
    import mcp_server

    known_src_idents = {
        mcp_server._code_ident("module", file_path) for file_path in file_entities
    }

    live = set()
    for edge in edges:
        if edge["src"] not in known_src_idents:
            continue
        vf_positions = invert_ms_to_positions(edge["vf_ms"], ts_positions)
        vt_positions = (
            None if edge["vt_ms"] >= VALID_TIME_FOREVER_MS
            else invert_ms_to_positions(edge["vt_ms"], ts_positions)
        )
        if edge_live_at(vf_positions, vt_positions, w):
            live.add((edge["src"], edge["dep"]))
    return live
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_at_scale_dep_preload_probe.py -v`
Expected: all pass.

- [ ] **Step 5: Add the edge loader and the sweep**

Append to `evals/at_scale/probe_dep_preload_exposure.py`:

```python
def load_dep_edges(db) -> List[Dict]:
    """Every :depends-on fact in the graph, current and historical, with its
    validity window.

    Mirrors _preload_known_deps' own query shape exactly, including the clause
    ORDER: [?src :ident ?srci] must precede [?src :depends-on ?dep], because
    minigraf's :db/valid-from / :db/valid-to pseudo-attributes bind to
    whichever EAV clause on ?src most recently precedes them. Putting :ident
    between :depends-on and the pseudo-attributes would bind ?vf to the :ident
    fact's own valid-from instead -- wrong, and silently so.
    """
    import json

    import mcp_server

    raw = mcp_server._db_execute(
        db,
        "(query [:find ?srci ?dep ?vf ?vt "
        ":any-valid-time "
        ":where [?src :ident ?srci] "
        "[?src :depends-on ?dep] "
        "[?src :db/valid-from ?vf] "
        "[?src :db/valid-to ?vt]])",
    )
    return [
        {"src": src, "dep": dep, "vf_ms": int(vf), "vt_ms": int(vt)}
        for src, dep, vf, vt in json.loads(raw).get("results", [])
    ]


def sweep(
    db,
    repo_path: str,
    linearization: List[str],
    commit_metadata: Sequence[CommitMeta],
) -> Dict:
    """Drive the REAL preload functions at each affected position and diff
    against the oracle.

    Calls the functions under test rather than a restatement of what we
    believe they do. On the #238 branch a reviewer and an implementer both
    simulated the counterfactual with a date bound instead of the real
    position-filtered one, which made an inadequate test look adequate and
    produced a false "bug not reachable" conclusion -- two fix rounds lost.
    """
    import mcp_server

    if len(commit_metadata) != len(linearization):
        raise ValueError(
            f"commit_metadata has {len(commit_metadata)} entries but "
            f"linearization has {len(linearization)}; a misaligned pair "
            "mis-filters the entire sweep"
        )
    for i, ((meta_hash, _ts, _a, _s), lin_hash) in enumerate(
        zip(commit_metadata, linearization)
    ):
        if meta_hash != lin_hash:
            raise ValueError(
                f"commit_metadata[{i}] is {meta_hash} but linearization[{i}] "
                f"is {lin_hash}; the two must be positionally aligned"
            )

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    ts_positions = build_ts_positions(commit_metadata)
    envelopes = resume_envelopes(commit_metadata)
    timestamps = [ts for _h, ts, _a, _s in commit_metadata]
    edges = load_dep_edges(db)

    unmappable = sum(
        1 for e in edges if not invert_ms_to_positions(e["vf_ms"], ts_positions)
    )
    collisions = {ts: pos for ts, pos in ts_positions.items() if len(pos) > 1}

    per_position = []
    for w in affected_positions(commit_metadata):
        (
            _entity_valid_from, _entity_descriptions, _entity_introduced_by,
            file_entities, _submodule_paths,
        ) = mcp_server._preload_known_entities(
            db, repo_path, valid_at=envelopes[w],
            hash_to_pos=hash_to_pos, watermark_pos=w,
        )
        _file_deps, dep_valid_from = mcp_server._preload_known_deps(
            db, file_entities,
            valid_at_ms=mcp_server._iso_to_epoch_ms(timestamps[w]),
        )
        actual = set(dep_valid_from.keys())
        expected = position_exact_live_edges(edges, ts_positions, file_entities, w)

        wrongly_included = sorted(actual - expected)
        wrongly_excluded = sorted(expected - actual)
        if wrongly_included or wrongly_excluded:
            per_position.append({
                "position": w,
                "commit": linearization[w],
                "date": timestamps[w],
                "wrongly_included": [list(e) for e in wrongly_included],
                "wrongly_excluded": [list(e) for e in wrongly_excluded],
            })

    return {
        "repo_path": repo_path,
        "commits": len(linearization),
        "dep_edges_total": len(edges),
        "affected_positions": affected_positions(commit_metadata),
        "misclassifying_positions": per_position,
        "wrongly_included_total": sum(
            len(p["wrongly_included"]) for p in per_position
        ),
        "wrongly_excluded_total": sum(
            len(p["wrongly_excluded"]) for p in per_position
        ),
        "timestamp_collisions": len(collisions),
        "unmappable_valid_from_facts": unmappable,
        "gitlink_events": gitlink_event_count(repo_path),
    }
```

- [ ] **Step 6: Add `main` and the ingestion driver**

Append to `evals/at_scale/probe_dep_preload_exposure.py`:

```python
async def _ingest_into(repo_path: str, branch: Optional[str], graph_path) -> str:
    """Ingest repo_path into a fresh scratch graph, using the plain in-process
    path.

    Deliberately NOT run_ingestion_benchmark: its in-flight poller starved the
    ingestion it measured (#242, fixed on this same branch). This probe needs
    a completed ingestion, not a measured one, so it takes the simplest path
    and no poller at all.
    """
    import mcp_server

    mcp_server._db = None
    mcp_server._graph_path = None
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
    resolved_branch = branch or mcp_server._default_git_branch(repo_path)
    await mcp_server._run_ingestion(repo_path, resolved_branch)
    return resolved_branch


def main() -> int:
    import argparse
    import asyncio
    import json
    import sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).parent.parent.parent.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    parser = argparse.ArgumentParser(
        description="Measure #245's :depends-on preload exposure against real history."
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    import frontier_registry
    import mcp_server

    with tempfile.TemporaryDirectory(prefix="minigraf-245-probe-") as tmpdir:
        graph_path = Path(tmpdir) / "probe.graph"
        branch = asyncio.run(_ingest_into(args.repo_path, args.branch, graph_path))

        linearization = frontier_registry.build_linearization(args.repo_path, branch)
        commit_metadata = mcp_server._git_commits(args.repo_path, None, branch)
        db = mcp_server._db if mcp_server._db is not None else mcp_server.open_db(str(graph_path))
        report = sweep(db, args.repo_path, linearization, commit_metadata)

    print(json.dumps(report, indent=2))
    print()
    print(f"commits:                       {report['commits']}")
    print(f":depends-on facts:             {report['dep_edges_total']}")
    print(f"structurally affected W:       {len(report['affected_positions'])}")
    print(f"W actually misclassifying:     {len(report['misclassifying_positions'])}")
    print(f"  wrongly INCLUDED edges:      {report['wrongly_included_total']}")
    print(f"  wrongly EXCLUDED edges:      {report['wrongly_excluded_total']}")
    print(f"timestamp collisions:          {report['timestamp_collisions']}")
    print(f"unmappable :valid-from facts:  {report['unmappable_valid_from_facts']}")
    print(f"gitlink events:                {report['gitlink_events']}")
    if report["gitlink_events"] == 0:
        print()
        print(
            "NOTE: zero gitlink events -- this history produces no :pinned-commit\n"
            "facts, so #245's :pinned-commit half is UNMEASURABLE here. That is\n"
            "not the same as zero risk; its field exposure remains unknown."
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Verify the module imports cleanly and the CLI parses**

Run: `python -c "from evals.at_scale import probe_dep_preload_exposure as p; print(p.main.__name__)"`
Expected: prints `main`, no ImportError.

Run: `python -m evals.at_scale.probe_dep_preload_exposure --help`
Expected: argparse help text, exit 0.

- [ ] **Step 8: Run the full test suite**

Run: `pytest tests/test_at_scale_dep_preload_probe.py tests/test_at_scale_ingestion_benchmark.py tests/test_at_scale_report.py -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add evals/at_scale/probe_dep_preload_exposure.py tests/test_at_scale_dep_preload_probe.py
git commit -m "$(cat <<'EOF'
Add the #245 exposure probe driver

Ingests into a scratch graph, then drives the REAL _preload_known_entities and
_preload_known_deps at each structurally affected position and diffs their
output against a position-exact oracle. Calling the functions under test
rather than restating their semantics is deliberate: on the #238 branch a
date-bound counterfactual made an inadequate test look adequate and cost two
fix rounds.

The oracle holds _preload_known_deps' own ident_to_file narrowing fixed --
that filter is already position-correct after #238 -- leaving the ts(W)
:depends-on bound as the single variable under measurement.

Reports timestamp collisions and unmappable :valid-from facts alongside the
exposure counts, so a smeared measurement is visible as one. Zero gitlink
events is reported as UNMEASURABLE, not as zero risk.

Measures only. Refs #245, refs #238, refs #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01T8qWT72sj5ja3wvkcgkK6b
EOF
)"
```

---

### Task 5: Run the measurement and record the findings

**Files:**
- Create: `evals/at_scale/results/245-dep-preload-exposure.json`

**Interfaces:**
- Consumes: `main()` from Task 4.
- Produces: the number this whole branch exists to obtain, plus issue comments.

- [ ] **Step 1: Run the probe against this repository**

Run:

```bash
python -m evals.at_scale.probe_dep_preload_exposure \
  --repo-path . \
  --json-out evals/at_scale/results/245-dep-preload-exposure.json
```

Expect roughly 25-40 minutes for the ingestion, then a fast sweep. Run it in the background and poll with `ps -p <PID>` — **not** `pgrep -f`, which matches the polling shell's own wrapper and never goes false.

- [ ] **Step 2: Sanity-check the output before believing it**

Three checks, in order. Any failure means the number is not yet trustworthy — stop and report rather than recording it:

1. `unmappable_valid_from_facts` should be 0. A non-zero count means the inversion assumption is broken and the exposure figure is unreliable, not merely smaller.
2. `gitlink_events` must be 0, confirming the spec's structural finding about `:pinned-commit`.
3. `affected_positions` should be a small set clustered in the low-to-mid 100s, consistent with the five inverted commits at positions 124-128 recorded in the spec. A count in the hundreds means `affected_positions` is over-selecting and needs re-checking against Task 3's tests.

- [ ] **Step 3: Commit the result**

```bash
git add evals/at_scale/results/245-dep-preload-exposure.json
git commit -m "$(cat <<'EOF'
Record the #245 :depends-on exposure measurement

Refs #245

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01T8qWT72sj5ja3wvkcgkK6b
EOF
)"
```

- [ ] **Step 4: Comment the findings on #245**

Post a comment carrying, verbatim from the JSON: the affected-position count, the misclassifying-position count, wrongly-included and wrongly-excluded edge totals, the collision count, the unmappable count, and the `:pinned-commit` structural result.

State plainly which of #245's three options the number argues for — or that it argues for accepting the residual — **without building it**. Include the two caveats: the oracle resolves ambiguity away from understating exposure, so the figure is an upper bound within its collision set; and `:pinned-commit` remains unmeasured, so this number speaks only for `:depends-on`.

Do not use a closing keyword. #245 stays open for the decision the number feeds.

- [ ] **Step 5: Open the PR**

```bash
git push -u origin probe-245-dep-preload-exposure
```

PR title: `Measure #245's :depends-on preload exposure, and fix the benchmark poller (#242)`

The PR body must use `Closes #242` and `Refs #245`, `Refs #238`, `Refs #222`. Never write a closing keyword beside #238 or #245 in any form, negated or otherwise.

Note in the body that `master` requires an approving review on top of green CI, and ask before using `--admin` to bypass.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `evals/at_scale/probe_dep_preload_exposure.py` | 3, 4 |
| `run_ingestion_benchmark.py` poller fix | 1 |
| `benchmark.md` note | 2 |
| Tests for both | 1, 2, 3, 4 |
| Issue comments recording findings | 5 |
| Out of scope: no preload/fact-model change | Global Constraints; no task touches `mcp_server.py` |
| `Refs`/`Closes` keyword discipline | Global Constraints; Tasks 1, 5 |
| Affected-`W` exact definition | Task 3, `affected_positions` |
| Oracle inverts both `valid_from` and `valid_to` | Task 3 `edge_live_at`; Task 4 `position_exact_live_edges` |
| Oracle is not a candidate fix | Task 4, docstring |
| Collisions reported, not hidden | Task 3 `build_ts_positions`; Task 4 `sweep` |
| `:pinned-commit` structural check | Task 4 `gitlink_event_count`; Task 5 Step 2 |
| Output: JSON + human summary | Task 4 `main` |
| `poll_count` / `poll_duty_fraction` | Task 2 |
| Ablation mandatory | Task 1 Step 2 |
| Fail-loud error handling | Task 4 `sweep` alignment check; unmappable counted |

No gaps.

**Placeholder scan:** No TBD/TODO. Every code step carries runnable code; every test step carries the test body.

**Type consistency:** `commit_metadata` is `(hash, ts_iso, author, subject)` throughout, matching `_git_commits`. `invert_ms_to_positions` returns `List[int]` everywhere it is consumed. `_poll_during_ingestion`'s 3-tuple is introduced in Task 1 and consumed in Task 2 with matching names. `file_entities` is `Dict[str, List[str]]` in both `_preload_known_deps` and `position_exact_live_edges`. Edge dicts use `src`/`dep`/`vf_ms`/`vt_ms` in Tasks 3 and 4 alike.

**Referenced `mcp_server` symbols, all verified present before this plan was finalized:**

| Symbol | Location |
|---|---|
| `_default_git_branch` | `mcp_server.py:4089` |
| `_iso_to_epoch_ms` | `mcp_server.py:4925` |
| `_git_commits` | `mcp_server.py:4112` |
| `_code_ident` | `mcp_server.py:4067` |
| `_preload_known_entities` | `mcp_server.py:7078` |
| `_preload_known_deps` | `mcp_server.py:7471` |
| `_valid_time_window_clauses` | `mcp_server.py:7453` |
| `_VALID_TIME_FOREVER_MS` | `mcp_server.py:7450` |
| `_db_execute` | `mcp_server.py:3280` |
| `frontier_registry.build_linearization` | `frontier_registry.py:26` |

The sentinel is `(1 << 63) - 1` (i64::MAX), **not** a year-9999 epoch value — an earlier draft of this plan had it wrong, which would have misread every open edge as closed at a nonsense position and understated exposure. Task 3's `VALID_TIME_FOREVER_MS` and Task 4's test literals both use `(1 << 63) - 1`.
