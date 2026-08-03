"""Where does _retract's 73.5% actually go?

The raw minigraf retract is free (0.06 ms/fact, no graph-size scaling --
see retract_microbench.py). But a full ingestion spends 4,138 s of 5,627 s
inside mcp_server._retract. That wrapper does three things:

    raw = _db_execute(db, f"(retract {facts})")        # measured: free
    triples = _resolved_facts_triples(facts, db)       # a _query_ident per
                                                       # non-keyword entity
    _index_write("delete", triples, index_con)         # fact_index.delete_facts

fact_index.delete_facts issues, per triple:

    DELETE FROM facts_fts WHERE entity=? AND attribute=? AND value=?
                          AND valid_to IS NULL

facts_fts is an FTS5 virtual table (fact_index.py:23). FTS5 has no B-tree on
its columns, so equality predicates cannot seek -- this is a full scan of the
index per deleted triple. fact_index.py:31 already says as much ("an O(n)
scan over facts_fts"), which is exactly why facts_dedup was added for the
INSERT path. The DELETE path never got the same treatment.

Prediction if that is the cause: delete cost per triple grows linearly with
the number of rows in facts_fts, and is unaffected by how many triples are
batched into one delete_facts call.

    .venv/bin/python scratchpad/index_delete_bench.py
"""
import os
import shutil
import sys
import tempfile
import time

REPO = "/home/aditya/Work/AMC/Minigraf/temporal_reasoning"
sys.path.insert(0, REPO)

import fact_index

TS = "2026-01-01T00:00:00Z"


def build_index(tmpdir, name, n_rows):
    path = os.path.join(tmpdir, f"{name}.sqlite3")
    con = fact_index.open_writer(path)
    rows = [
        (f":e/n{i}", ":attr", f"value number {i} with some text", TS, None)
        for i in range(n_rows)
    ]
    for start in range(0, n_rows, 2000):
        fact_index.insert_facts(con, rows[start:start + 2000])
    con.commit()
    return con, path


def bench_delete(con, n_delete, per_call):
    rows = [
        (f":e/n{i}", ":attr", f"value number {i} with some text", TS, None)
        for i in range(n_delete)
    ]
    t0 = time.perf_counter()
    for start in range(0, n_delete, per_call):
        fact_index.delete_facts(con, rows[start:start + per_call])
    con.commit()
    return time.perf_counter() - t0


def main(tmpdir):
    print("=== D1: delete 200 rows one call, vary index size ===")
    print(f"{'index rows':>11} {'seconds':>9} {'ms/triple':>11}")
    prev = None
    for n_rows in (5_000, 20_000, 80_000):
        con, _ = build_index(tmpdir, f"d1_{n_rows}", n_rows)
        secs = bench_delete(con, 200, 200)
        per = secs / 200 * 1000
        growth = f"  ({per / prev:.1f}x)" if prev else ""
        print(f"{n_rows:>11} {secs:>9.3f} {per:>11.3f}{growth}")
        prev = per
        con.close()

    print("\n=== D2: fixed 80,000-row index, delete 200, vary triples/call ===")
    print(f"{'triples/call':>13} {'calls':>7} {'seconds':>9} {'ms/triple':>11}")
    for per_call in (1, 10, 200):
        con, _ = build_index(tmpdir, f"d2_{per_call}", 80_000)
        secs = bench_delete(con, 200, per_call)
        n_calls = (200 + per_call - 1) // per_call
        print(f"{per_call:>13} {n_calls:>7} {secs:>9.3f} {secs / 200 * 1000:>11.3f}")
        con.close()

    print("\n=== D3: contrast -- insert the same volumes ===")
    print(f"{'index rows':>11} {'seconds':>9} {'ms/triple':>11}")
    for n_rows in (5_000, 20_000, 80_000):
        con, _ = build_index(tmpdir, f"d3_{n_rows}", n_rows)
        new = [
            (f":new/n{i}", ":attr", f"fresh value {i} with some text", TS, None)
            for i in range(200)
        ]
        t0 = time.perf_counter()
        fact_index.insert_facts(con, new)
        con.commit()
        secs = time.perf_counter() - t0
        print(f"{n_rows:>11} {secs:>9.3f} {secs / 200 * 1000:>11.3f}")
        con.close()


if __name__ == "__main__":
    tmpdir = tempfile.mkdtemp(prefix="idx-del-bench-")
    try:
        main(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\ndone")
