"""#241 checkpoint cost probes: scaling and durability.

Two one-off measurements behind the checkpoint-cadence design doc's "Summary
of findings" (docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md):

1. SCALING -- checkpoint() is O(graph size) WAL compaction, NOT an
   incremental flush proportional to how much was written since the last
   one. At each of three plateaus (5,000 / 20,000 / 50,000 facts), the graph
   is checkpointed once to establish a clean baseline, then timed twice more
   from that same baseline: once after writing a single fact, once after
   writing a 5,000-fact batch. If checkpoint cost depended on dirty bytes,
   the batch column would dwarf the single-fact column as the batch grows;
   if it depends only on total graph size, the two columns stay close (the
   design doc's own run put them within ~10-30% of each other at every
   plateau, narrowing as the graph grows and per-checkpoint cost comes to
   dominate the two writes' own cost).

2. DURABILITY -- checkpoint() is NOT a durability boundary. A real child
   PROCESS (not a thread, not os.fork() inside this interpreter -- see
   tests/test_mcp_server.py's _hold_lock_subprocess docstring on why this
   codebase insists on genuine subprocess-manufactured state for
   lock/file-durability claims) transacts a checkpointed fact, an
   uncheckpointed fact, and an uncheckpointed :ingestion/watermark fact --
   the shape a mid-Stage-B ingestion crash would leave under the duty-cycle
   policy -- then dies via os._exit(9): no atexit, no flush, no destructors,
   no chance for minigraf's Drop impl to run. The parent then reopens the
   same graph file fresh and asserts all three facts recovered anyway,
   because every transact is already durable via <graph>.wal the moment it
   returns; checkpoint() only ever compacts that log into the main file.

Neither probe changes production code, and neither is a regression test --
no assertion here gates CI. docs/testing-conventions.md's real-backend rule
governs tests/test_mcp_server.py, not one-off probes like this one; the
precedent for probes living outside that suite, with their results recorded
as JSON rather than asserted in pytest, is #245's probe_dep_preload_exposure.py.

NOTE on minigraf's execute() grammar, easy to get wrong: a raw db.execute()
call needs the COMMAND form -- "(query [:find ...])", not a bare
"[:find ...]" -- or it raises "Expected a list starting with a command
symbol". Likewise "(transact {opts} facts)" and "(retract facts)"; opts use
literal Clojure-map braces, e.g. {:valid-from "2026-01-01T00:00:00Z"}.

Self-isolating: everything runs against its own tempdir. MINIGRAF_GRAPH_PATH
and MINIGRAF_INDEX_PATH are pointed at a scratch path inside it before any
minigraf import, even though this probe talks to MiniGrafDb directly and
never reads those env vars itself -- matching the convention the sibling
benchmarks in this directory already follow (e.g. bench_retract_cost.py) so
that a stray import of mcp_server anywhere in the dependency chain cannot
resolve to memory.graph in the repo root. Results are written to
results/241-checkpoint-cost.json, following #245's probe_dep_preload_exposure.py
convention of recording a probe's numbers as a committed JSON artifact.

    .venv/bin/python evals/at_scale/probe_checkpoint_cost.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = "/home/aditya/Work/AMC/Minigraf/temporal_reasoning"
sys.path.insert(0, REPO)

TS = "2026-01-01T00:00:00Z"

SCALING_PLATEAUS = (5_000, 20_000, 50_000)
SCALING_BATCH = 5_000
SEED_CHUNK = 2_000


def _facts(lo: int, hi: int) -> str:
    return " ".join(f'[:e/n{i} :attr "value number {i} with some text"]' for i in range(lo, hi))


def _transact(db, lo: int, hi: int) -> None:
    db.execute(f'(transact {{:valid-from "{TS}"}} [{_facts(lo, hi)}])')


def scaling_experiment(tmpdir: str) -> list:
    """Grow one graph in 5,000-fact plateaus; at each, checkpoint after a
    single fact and again after a 5,000-fact batch, both timed from the same
    freshly-checkpointed baseline. Returns one row per plateau.
    """
    from minigraf import MiniGrafDb

    path = os.path.join(tmpdir, "scaling.graph")
    db = MiniGrafDb.open(path)
    total = 0
    rows = []
    for plateau in SCALING_PLATEAUS:
        # Bulk-seed up to the plateau, batched, unmeasured -- then one
        # checkpoint to establish the clean baseline both timed checkpoints
        # below measure from.
        while total < plateau:
            hi = min(total + SEED_CHUNK, plateau)
            _transact(db, total, hi)
            total = hi
        db.checkpoint()
        graph_mb = os.path.getsize(path) / (1024 * 1024)

        _transact(db, total, total + 1)
        total += 1
        t0 = time.perf_counter()
        db.checkpoint()
        ckpt_after_1_ms = (time.perf_counter() - t0) * 1000.0

        _transact(db, total, total + SCALING_BATCH)
        total += SCALING_BATCH
        t0 = time.perf_counter()
        db.checkpoint()
        ckpt_after_batch_ms = (time.perf_counter() - t0) * 1000.0

        rows.append({
            "plateau_facts": plateau,
            "graph_mb": round(graph_mb, 2),
            "ckpt_after_1_fact_ms": round(ckpt_after_1_ms, 2),
            f"ckpt_after_{SCALING_BATCH}_facts_ms": round(ckpt_after_batch_ms, 2),
            "ratio_batch_over_1": (
                round(ckpt_after_batch_ms / ckpt_after_1_ms, 3) if ckpt_after_1_ms else None
            ),
        })
    del db
    return rows


def _durability_child_script(path: str) -> str:
    """Source for a child process that writes three facts -- one
    checkpointed, two not, one of them the :ingestion/watermark shape -- and
    then dies with no cleanup at all.

    Built by hand rather than str.format(): minigraf's own transact grammar
    uses literal Clojure-map braces ({:valid-from ...}), which collide with
    format()'s escaping rules. An f-string with doubled braces is used
    instead, which keeps the braces visually obvious in the source.
    """
    return (
        "import minigraf, os\n"
        f"db = minigraf.MiniGrafDb.open({path!r})\n"
        f'db.execute(\'(transact {{:valid-from "{TS}"}} '
        '[[:probe/checkpointed :attr "recovered-1"]])\')\n'
        "db.checkpoint()\n"
        f'db.execute(\'(transact {{:valid-from "{TS}"}} '
        '[[:probe/uncheckpointed :attr "recovered-2"]])\')\n'
        f'db.execute(\'(transact {{:valid-from "{TS}"}} '
        '[[:ingestion/watermark :hash "deadbeefcafe"]])\')\n'
        "os._exit(9)\n"
    )


def durability_experiment(tmpdir: str) -> dict:
    """Hard-kill a child mid-write and confirm every fact -- checkpointed or
    not -- survives, because durability lives in <graph>.wal, not in
    checkpoint().
    """
    path = os.path.join(tmpdir, "durability.graph")
    script = _durability_child_script(path)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
    )

    # Sizes are captured BEFORE the parent ever opens the file, so a
    # reopen's own compaction behaviour can't retroactively change what we
    # report the crash left behind.
    graph_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    wal_bytes = os.path.getsize(path + ".wal") if os.path.exists(path + ".wal") else 0

    from minigraf import MiniGrafDb

    t0 = time.perf_counter()
    db = MiniGrafDb.open(path)
    reopen_ms = (time.perf_counter() - t0) * 1000.0

    def _recovered(datalog_query: str) -> bool:
        raw = db.execute(datalog_query)
        return len(json.loads(raw).get("results", [])) > 0

    recovered_checkpointed = _recovered(
        "(query [:find ?v :where [:probe/checkpointed :attr ?v]])"
    )
    recovered_uncheckpointed = _recovered(
        "(query [:find ?v :where [:probe/uncheckpointed :attr ?v]])"
    )
    recovered_watermark = _recovered(
        "(query [:find ?h :where [:ingestion/watermark :hash ?h]])"
    )
    del db

    return {
        "child_exit_code": result.returncode,
        "child_died_via_exit9": result.returncode == 9,
        "child_stderr": result.stderr.strip(),
        "graph_bytes_after_hard_kill": graph_bytes,
        "wal_bytes_after_hard_kill": wal_bytes,
        "reopen_ms": round(reopen_ms, 2),
        "recovered_checkpointed_fact": recovered_checkpointed,
        "recovered_uncheckpointed_fact": recovered_uncheckpointed,
        "recovered_uncheckpointed_watermark": recovered_watermark,
        "all_three_recovered": (
            recovered_checkpointed and recovered_uncheckpointed and recovered_watermark
        ),
    }


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="ckpt-cost-probe-")
    os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(tmpdir, "unused.graph")
    os.environ["MINIGRAF_INDEX_PATH"] = os.path.join(tmpdir, "unused.graph.fts.sqlite3")
    try:
        print("=== Scaling: checkpoint cost vs graph size, flat in dirty bytes ===")
        scaling_dir = os.path.join(tmpdir, "scaling")
        os.makedirs(scaling_dir, exist_ok=True)
        scaling_rows = scaling_experiment(scaling_dir)
        print(f"{'facts':>8} {'graph MB':>9} {'ckpt after 1 fact':>18} "
              f"{'ckpt after ' + str(SCALING_BATCH) + ' facts':>22} {'ratio':>7}")
        for row in scaling_rows:
            print(f"{row['plateau_facts']:>8} {row['graph_mb']:>9.2f} "
                  f"{row['ckpt_after_1_fact_ms']:>15.1f} ms "
                  f"{row[f'ckpt_after_{SCALING_BATCH}_facts_ms']:>19.1f} ms "
                  f"{row['ratio_batch_over_1']:>7.2f}")

        print("\n=== Durability: survive a hard kill (os._exit(9)) mid-write ===")
        durability_dir = os.path.join(tmpdir, "durability")
        os.makedirs(durability_dir, exist_ok=True)
        durability = durability_experiment(durability_dir)
        for k, v in durability.items():
            print(f"  {k}: {v}")

        report = {
            "probe": "241-checkpoint-cost",
            "scaling": scaling_rows,
            "durability": durability,
        }

        results_path = Path(REPO) / "evals" / "at_scale" / "results" / "241-checkpoint-cost.json"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nWrote {results_path}")

        ok = True
        if not durability["all_three_recovered"]:
            print("\nFAIL: not all three facts recovered after the hard kill.")
            ok = False
        if not durability["child_died_via_exit9"]:
            print(
                f"\nFAIL: child did not exit via os._exit(9) "
                f"(returncode={durability['child_exit_code']!r}); "
                "durability result is not trustworthy."
            )
            ok = False
        for row in scaling_rows:
            ratio = row["ratio_batch_over_1"]
            if ratio is not None and not (0.5 <= ratio <= 2.0):
                print(
                    f"\nNOTE: plateau {row['plateau_facts']} ratio {ratio} is outside "
                    "the ~2x band the flat-in-dirty-bytes finding predicts; not a hard "
                    "failure, but worth a second look before trusting this run."
                )
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
