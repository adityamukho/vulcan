"""Does #235's per-candidate lineage check explain the 30x ingestion slowdown?

Commit 9d91593 (#235) fixed a data-corruption bug in _forward_apply by
dropping the state.provisional_idents prefilter: before the fix,
_lineage_is_provisional(db, ident) ran only for idents already in the
run-start snapshot (small, mostly HITS -- a marker entity exists); after the
fix it runs for EVERY candidate ident of every "A"/"M" file, and the
overwhelming majority are MISSES (no :type/lineage-marker companion entity
was ever created for that ident).

The at-scale ingestion benchmark on the fixed code was killed after 3h54m,
~1/3 complete; the same benchmark on the pre-fix commit took 1,600.55s for
568 commits -- roughly 30x. The box was unloaded, and every file was
byte-static for minutes while the process sat at 99.6% CPU: read-bound, not
write-bound.

HYPOTHESIS (mirrors #236's fact_index.delete_facts equality-scan finding):
a lookup for a NON-EXISTENT entity degrades to a scan rather than an index
seek, so per-call MISS cost grows with total graph size -- making the whole
run's cumulative cost shape quadratic.

_lineage_is_provisional (mcp_server.py, ~line 5210) issues one point query:
    (query [:find ?e :where [<marker-ident> :entity ?e]])
_preload_provisional_idents (mcp_server.py, ~line 7347) issues ONE query for
the complete provisional set:
    (query [:find ?e :where [?m :entity-type :type/lineage-marker] [?m :entity ?e]])

This script measures, against a real on-disk graph (no mocks):
  E1: HIT vs MISS cost for _lineage_is_provisional, across 3 graph sizes
      spanning 50x (100k / 1M / 5M filler facts).
  E2: _preload_provisional_idents cost as a function of (a) graph size and
      (b) marker population size (reverse stream creates ~1,265 provisional
      idents/commit, so this can grow to tens of thousands mid-run before
      _forward_reconcile_provisional drains it).
  E3: crossover at ~1,265 candidates/commit (the reverse stream's working
      figure) -- N point queries vs one preload call, at each graph size.

METHODOLOGY NOTE: fixture writes are checkpointed (db.checkpoint()) before
any timed query. Skipping this is a trap -- an uncheckpointed graph answers
both the point query and the join query incorrectly once enough facts have
been written since the last checkpoint (verified deterministic, and fully
fixed by inserting one checkpoint() call; the visibility limit appeared to
sit near 65,536 recently-written facts in one repro, but did not hold as a
clean threshold at larger sizes, consistent with an internal auto-checkpoint
eventually catching up on its own rather than a fixed-capacity bug). Real
_transact/_retract callers always checkpoint after a write
(_checkpoint_after_write), so checkpointing fixtures here matches production
behavior rather than working around a production bug -- this is a benchmark
fixture gotcha, not a #235-relevant finding, and every reported cell below
is checked against its expected result to catch a repeat of it.

    .venv/bin/python evals/at_scale/bench_lineage_query_cost.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

REPO = "/home/aditya/Work/AMC/Minigraf/temporal_reasoning"
sys.path.insert(0, REPO)

from minigraf import MiniGrafDb
import mcp_server

TS = "2026-01-01T00:00:00Z"

# The reverse stream's working figure for provisional idents/commit (task's
# assumption, stated explicitly per the brief).
CANDIDATES_PER_COMMIT = 1265


def fresh_db(tmpdir, name):
    """A real on-disk MiniGrafDb, like the other at-scale bench scripts."""
    path = os.path.join(tmpdir, f"{name}.graph")
    for suffix in ("", ".wal", ".lock"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return MiniGrafDb.open(path), path


def populate_filler(db, n, chunk=1000):
    """Bulk unrelated facts, distinct entities, to set total graph size --
    same shape as bench_retract_cost.py's populate(). These entities also
    double as realistic MISS targets below: they exist in the graph (as a
    real forward-walk candidate ident would) but were never given a lineage
    marker, exactly like the vast majority of #235's new per-candidate
    checks.
    """
    for start in range(0, n, chunk):
        batch = [f'[:e/n{i} :attr "v{i}"]' for i in range(start, min(start + chunk, n))]
        db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(batch) + "])")
    db.checkpoint()  # see module docstring's METHODOLOGY NOTE


def add_lineage_markers(db, entity_idents, chunk=1000):
    """Write the exact 3-fact marker pattern _lineage_confirm_batch writes,
    via mcp_server._lineage_marker_ident so the ident shape matches
    production byte-for-byte.
    """
    facts = []
    for ident in entity_idents:
        mident = mcp_server._lineage_marker_ident(ident)
        facts.append(f"[{mident} :entity-type :type/lineage-marker]")
        facts.append(f"[{mident} :entity {ident}]")
        facts.append(f"[{mident} :status :provisional]")
    for start in range(0, len(facts), chunk):
        batch = facts[start:start + chunk]
        db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(batch) + "])")
    db.checkpoint()  # see module docstring's METHODOLOGY NOTE


def timed_is_provisional(db, idents, reps, expected):
    """One warmup call (untimed) -- asserted against `expected` so a
    fixture-visibility bug (see module docstring) fails loudly instead of
    silently reporting a MISS-cost number as if it were a HIT, or vice
    versa -- then `reps` timed calls cycling through idents. Returns
    ms/call."""
    warmup = mcp_server._lineage_is_provisional(db, idents[0])
    assert warmup == expected, (
        f"_lineage_is_provisional({idents[0]!r}) = {warmup}, expected {expected} -- "
        "fixture not visible to queries yet (see module docstring's METHODOLOGY NOTE)"
    )
    t0 = time.perf_counter()
    for i in range(reps):
        mcp_server._lineage_is_provisional(db, idents[i % len(idents)])
    return (time.perf_counter() - t0) / reps * 1000


def timed_preload(db, reps, expected_len=None):
    """One warmup call (untimed), then `reps` timed calls. Returns
    (ms/call, result_len). When expected_len is given, asserts the warmup
    call's result count matches it -- same fixture-visibility guard as
    timed_is_provisional."""
    result = mcp_server._preload_provisional_idents(db)  # warmup
    if expected_len is not None:
        assert len(result) == expected_len, (
            f"_preload_provisional_idents returned {len(result)} idents, expected "
            f"{expected_len} -- fixture not visible to queries yet (see module "
            "docstring's METHODOLOGY NOTE)"
        )
    t0 = time.perf_counter()
    for _ in range(reps):
        result = mcp_server._preload_provisional_idents(db)
    return (time.perf_counter() - t0) / reps * 1000, len(result)


def experiment_1_and_2a(tmpdir):
    """E1: HIT vs MISS cost for _lineage_is_provisional, and E2a: preload
    cost, both across graph size (100k / 1M / 5M filler facts, 50x range).
    Fixed marker population = CANDIDATES_PER_COMMIT (1,265), on a namespace
    (:e/hit{i}) disjoint from the filler entities so HIT/MISS draw from
    consistent, unambiguous pools. REPS=300 for the point query (sub-ms/call,
    needs volume for a stable mean); REPS=20 for preload (single-digit-ms+
    per call, stable well before 20).
    """
    is_prov_reps = 300
    preload_reps = 20
    sizes = (100_000, 1_000_000, 5_000_000)
    results = {}  # size -> (hit_ms, miss_ms, preload_ms, preload_len)

    print("=== E1: _lineage_is_provisional, HIT vs MISS, vary graph size ===")
    print(f"({is_prov_reps} reps/cell, 1 warmup call before each timed loop; "
          f"{CANDIDATES_PER_COMMIT} markers planted per size)")
    print(f"{'filler facts':>13} {'hit ms/call':>12} {'miss ms/call':>13} {'miss/hit':>9}")
    for n in sizes:
        db, _ = fresh_db(tmpdir, f"e1_{n}")
        populate_filler(db, n)
        hit_idents = [f":e/hit{i}" for i in range(CANDIDATES_PER_COMMIT)]
        add_lineage_markers(db, hit_idents)
        # MISS: real existing entities (the filler pool) with no marker --
        # the realistic shape of #235's new per-candidate checks.
        miss_idents = [f":e/n{i}" for i in range(0, n, max(1, n // is_prov_reps))]

        hit_ms = timed_is_provisional(db, hit_idents, is_prov_reps, expected=True)
        miss_ms = timed_is_provisional(db, miss_idents, is_prov_reps, expected=False)
        preload_ms, preload_len = timed_preload(db, preload_reps, expected_len=CANDIDATES_PER_COMMIT)

        results[n] = (hit_ms, miss_ms, preload_ms, preload_len)
        print(f"{n:>13} {hit_ms:>12.4f} {miss_ms:>13.4f} {miss_ms / hit_ms:>8.2f}x")
        del db

    print("\n=== E2a: _preload_provisional_idents, same graphs as E1 ===")
    print(f"({preload_reps} reps/cell, 1 warmup call before each timed loop)")
    print(f"{'filler facts':>13} {'markers':>8} {'preload ms/call':>16} {'result len':>10}")
    for n in sizes:
        hit_ms, miss_ms, preload_ms, preload_len = results[n]
        print(f"{n:>13} {CANDIDATES_PER_COMMIT:>8} {preload_ms:>16.3f} {preload_len:>10}")

    return results


def experiment_2b(tmpdir):
    """E2b: _preload_provisional_idents cost as a function of marker
    population size, graph size held fixed at 1,000,000 filler facts.
    Marker counts approximate 0 / 1 / 5 / 10 commits' worth of UNRECONCILED
    provisionals sitting in the graph. result_len is asserted equal to the
    marker count at every row (see module docstring's METHODOLOGY NOTE).
    """
    preload_reps = 20
    print("\n=== E2b: _preload_provisional_idents, vary marker population "
          "(graph fixed at 1,000,000 filler facts) ===")
    print(f"({preload_reps} reps/cell, 1 warmup call before each timed loop)")
    print(f"{'markers':>8} {'preload ms/call':>16} {'result len':>10}")
    db, _ = fresh_db(tmpdir, "e2b")
    populate_filler(db, 1_000_000)
    marker_counts = (0, CANDIDATES_PER_COMMIT, 5 * CANDIDATES_PER_COMMIT, 10 * CANDIDATES_PER_COMMIT)
    prev = 0
    rows = []
    for target in marker_counts:
        if target > prev:
            add_lineage_markers(db, [f":e/hit{i}" for i in range(prev, target)])
        prev = target
        preload_ms, preload_len = timed_preload(db, preload_reps, expected_len=target)
        rows.append((target, preload_ms, preload_len))
        print(f"{target:>8} {preload_ms:>16.3f} {preload_len:>10}")
    del db
    return rows


def experiment_3(e1_results):
    """E3: crossover at CANDIDATES_PER_COMMIT (~1,265) candidates/commit --
    N point queries via _lineage_is_provisional vs ONE
    _preload_provisional_idents call, at each E1 graph size.

    Assumption (stated per the brief): candidates are modeled as ALL MISS.
    That is the "overwhelming majority" case the #235 diff docstring
    describes, and it is also the more expensive of the two point-query
    costs in every E1 row measured above, so it is the conservative
    (worst-case-for-preload) choice for this comparison.
    """
    print("\n=== E3: crossover at "
          f"{CANDIDATES_PER_COMMIT} candidates/commit (assumed all-MISS) ===")
    print(f"{'filler facts':>13} {'N x miss_ms':>13} {'1x preload_ms':>14} {'winner':>10} {'margin':>8}")
    prev_winner = None
    flipped = False
    for n, (hit_ms, miss_ms, preload_ms, preload_len) in e1_results.items():
        pointwise_total = CANDIDATES_PER_COMMIT * miss_ms
        winner = "preload" if preload_ms < pointwise_total else "pointwise"
        margin = max(pointwise_total, preload_ms) / min(pointwise_total, preload_ms)
        print(f"{n:>13} {pointwise_total:>13.2f} {preload_ms:>14.2f} {winner:>10} {margin:>7.2f}x")
        if prev_winner is not None and winner != prev_winner:
            flipped = True
        prev_winner = winner
    print(f"\ncrossover verdict: winner {'FLIPS' if flipped else 'does NOT flip'} "
          "across the graph sizes measured.")


if __name__ == "__main__":
    tmpdir = tempfile.mkdtemp(prefix="lineage-query-bench-")
    os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(tmpdir, "unused.graph")
    try:
        e1_results = experiment_1_and_2a(tmpdir)
        experiment_2b(tmpdir)
        experiment_3(e1_results)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\ndone")
