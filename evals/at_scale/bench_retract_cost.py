"""Microbenchmark: what does minigraf's retract actually cost?

Decides one question: is retract cost per-FACT, per-CALL, or dependent on
total graph size? That determines whether batching
_correction_sweep_apply's 72,971 individual :modified-in retracts (1,629 s,
29% of a full ingestion run) can recover anything.

Experiment A -- fixed graph, vary facts-per-call.
  Retract the same 1,000 facts as 1000x1, 100x10, 10x100, 1x1000.
  Total time roughly constant  -> pure per-fact cost, batching is useless.
  Total time falls with fewer calls -> a per-call component exists, batching wins.

Experiment B -- fixed call shape, vary graph size.
  Retract 100 facts one-at-a-time from graphs of 2k / 10k / 40k facts.
  Per-retract cost rising with graph size -> retract scans, and the real
  problem is graph size, not call shape.

Experiment C -- transact, same shape as A, for contrast.

    .venv/bin/python scratchpad/retract_microbench.py
"""
import os
import shutil
import sys
import tempfile
import time

REPO = "/home/aditya/Work/AMC/Minigraf/temporal_reasoning"
sys.path.insert(0, REPO)

TS = "2026-01-01T00:00:00Z"


def fresh_db(tmpdir, name):
    """A real on-disk MiniGrafDb, like the benchmark uses."""
    from minigraf import MiniGrafDb
    path = os.path.join(tmpdir, f"{name}.graph")
    for suffix in ("", ".wal", ".lock"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return MiniGrafDb.open(path), path


def facts_for(lo, hi):
    """Distinct entities so nothing collides in the EAVT index."""
    return [f'[:e/n{i} :attr "v{i}"]' for i in range(lo, hi)]


def populate(db, n, chunk=500):
    """Seed the graph with n facts, in chunks so the seeding itself is fast."""
    for start in range(0, n, chunk):
        batch = facts_for(start, min(start + chunk, n))
        db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(batch) + "])")


def timed_retract(db, groups):
    """Retract each group as ONE call. Returns (seconds, n_calls, n_facts)."""
    n_calls = n_facts = 0
    t0 = time.perf_counter()
    for g in groups:
        db.execute("(retract [" + " ".join(g) + "])")
        n_calls += 1
        n_facts += len(g)
    return time.perf_counter() - t0, n_calls, n_facts


def chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def experiment_a(tmpdir):
    print("\n=== A: fixed graph (10,000 facts), retract 1,000, vary facts/call ===")
    print(f"{'shape':>16} {'calls':>7} {'facts':>7} {'seconds':>9} {'ms/call':>10} {'ms/fact':>9}")
    for per_call in (1, 10, 100, 1000):
        db, path = fresh_db(tmpdir, f"a{per_call}")
        populate(db, 10_000)
        targets = facts_for(0, 1000)
        secs, n_calls, n_facts = timed_retract(db, chunks(targets, per_call))
        print(f"{per_call:>10} f/call {n_calls:>7} {n_facts:>7} {secs:>9.2f} "
              f"{secs / n_calls * 1000:>10.1f} {secs / n_facts * 1000:>9.2f}")
        del db


def experiment_b(tmpdir):
    print("\n=== B: retract 100 facts one-at-a-time, vary graph size ===")
    print(f"{'graph facts':>12} {'seconds':>9} {'ms/retract':>12}")
    for graph_size in (2_000, 10_000, 40_000):
        db, path = fresh_db(tmpdir, f"b{graph_size}")
        populate(db, graph_size)
        targets = facts_for(0, 100)
        secs, n_calls, _ = timed_retract(db, chunks(targets, 1))
        print(f"{graph_size:>12} {secs:>9.2f} {secs / n_calls * 1000:>12.1f}")
        del db


def experiment_c(tmpdir):
    print("\n=== C: transact, same shape as A, for contrast ===")
    print(f"{'shape':>16} {'calls':>7} {'facts':>7} {'seconds':>9} {'ms/call':>10} {'ms/fact':>9}")
    for per_call in (1, 10, 100, 1000):
        db, path = fresh_db(tmpdir, f"c{per_call}")
        populate(db, 10_000)
        new = [f'[:new/n{i} :attr "v{i}"]' for i in range(1000)]
        n_calls = n_facts = 0
        t0 = time.perf_counter()
        for g in chunks(new, per_call):
            db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(g) + "])")
            n_calls += 1
            n_facts += len(g)
        secs = time.perf_counter() - t0
        print(f"{per_call:>10} f/call {n_calls:>7} {n_facts:>7} {secs:>9.2f} "
              f"{secs / n_calls * 1000:>10.1f} {secs / n_facts * 1000:>9.2f}")
        del db


if __name__ == "__main__":
    tmpdir = tempfile.mkdtemp(prefix="retract-bench-")
    os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(tmpdir, "unused.graph")
    try:
        experiment_a(tmpdir)
        experiment_b(tmpdir)
        experiment_c(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\ndone")
