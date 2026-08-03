"""Instrumented micro-harness for #233 -- per-call-site write attribution
for the reverse-bulk-fill walk (Stage A).

Drives N commits of the reverse walk directly, wrapping _transact/_retract
with counters and timers keyed by calling function, so the walk's write
amplification can be READ per call site rather than inferred from the graph
size after the fact. The full at-scale ingestion benchmark
(run_ingestion_benchmark.py) reports the aggregate; this reports where it
goes, on a slice small enough to iterate on.

Self-isolating: creates its own tempdir and points MINIGRAF_GRAPH_PATH and
MINIGRAF_INDEX_PATH at it, so it never touches memory.graph.

    .venv/bin/python evals/at_scale/profile_reverse_walk_writes.py [N] [REPO] [BRANCH]
"""
import collections
import os
import sys
import tempfile
import time
import traceback

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
REPO = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
BRANCH = sys.argv[3] if len(sys.argv) > 3 else "HEAD"
sys.path.insert(0, REPO)

tmpdir = tempfile.mkdtemp(prefix="reverse-prof-")
os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(tmpdir, "bench.graph")
os.environ["MINIGRAF_INDEX_PATH"] = os.path.join(tmpdir, "bench.graph.fts.sqlite3")

import mcp_server as m
import frontier_registry

counts = collections.Counter()
timings = collections.Counter()


def _caller():
    st = traceback.extract_stack()
    # st[-1] is _caller, st[-2] is the wrapper, st[-3] is the real caller
    return st[-3].name


_real_transact = m._transact
_real_retract = m._retract


def _t(*a, **k):
    c = _caller()
    t0 = time.perf_counter()
    try:
        return _real_transact(*a, **k)
    finally:
        counts[(c, "transact")] += 1
        timings[(c, "transact")] += time.perf_counter() - t0


def _r(*a, **k):
    c = _caller()
    t0 = time.perf_counter()
    try:
        return _real_retract(*a, **k)
    finally:
        counts[(c, "retract")] += 1
        timings[(c, "retract")] += time.perf_counter() - t0


m._transact = _t
m._retract = _r

_real_db_execute = m._db_execute
query_counts = collections.Counter()
query_timings = collections.Counter()


def _dbx(db, datalog):
    if datalog.startswith("(query"):
        c = traceback.extract_stack()[-2].name
        t0 = time.perf_counter()
        try:
            return _real_db_execute(db, datalog)
        finally:
            query_counts[c] += 1
            query_timings[c] += time.perf_counter() - t0
    return _real_db_execute(db, datalog)


m._db_execute = _dbx

db = m._open_db_at(os.environ["MINIGRAF_GRAPH_PATH"])
commits = m._git_commits(REPO, None, BRANCH)
linearization = [c[0] for c in commits]
print(f"{len(linearization)} commits in history; walking {N} from the tip")

alloc = frontier_registry.FrontierAllocator(len(linearization))
index_con = None

t0 = time.perf_counter()
per_commit = []
for i in range(N):
    c0 = time.perf_counter()
    before = sum(counts.values())
    h = m._reverse_fill_claim_and_process(
        db, REPO, linearization, commits, alloc, index_con=index_con,
    )
    if h is None:
        break
    per_commit.append((h[:12], sum(counts.values()) - before, time.perf_counter() - c0))
elapsed = time.perf_counter() - t0

total = sum(counts.values())
print(f"\n{len(per_commit)} commits in {elapsed:.1f}s -> {elapsed/max(1,len(per_commit)):.2f}s/commit")
print(f"total writes: {total} -> {total/max(1,len(per_commit)):.0f} tx/commit\n")

print(f"{'call site':<42} {'op':<9} {'count':>8} {'sec':>8}")
for (site, op), n in counts.most_common():
    print(f"{site:<42} {op:<9} {n:>8} {timings[(site, op)]:>8.1f}")

print(f"\n{'query call site':<42} {'count':>8} {'sec':>8}")
for site, n in query_counts.most_common(15):
    print(f"{site:<42} {n:>8} {query_timings[site]:>8.1f}")

print("\nper-commit writes:")
for h, n, secs in per_commit:
    print(f"  {h}  {n:>7} writes  {secs:>7.2f}s")

print(f"\ngraph: {os.path.getsize(os.environ['MINIGRAF_GRAPH_PATH'])} B  ({tmpdir})")
