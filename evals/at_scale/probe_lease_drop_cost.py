#!/usr/bin/env python3
"""#260 attribution: how often is the DB handle dropped, and what does it cost?

#260's fit put ~0.54 s/commit of FIXED-cost growth outside every mechanism its
own instrument could see. `_CheckpointPolicy`'s explicit checkpoints are
recorded in the trace's `ckpt_d_seconds` and account for ~10% of the rise; the
rest was unattributed.

THIS PROBE'S TARGET. `_run_ingestion` wraps every commit in `async with
db_lease_async() as db:`. `_DbLeaseManager.release()` frees the handle at
refcount 1 -> 0, and dropping a `MiniGrafDb` runs a full `do_checkpoint` inside
minigraf's `Drop for Inner` (`src/db.rs:196`) -- O(graph size) regardless of
dirty bytes (project-minigraf/minigraf#315). Two things make that invisible to
#260's trace:

  * `db_lease_async()`'s `finally` calls `release()` BEFORE `apply_s`'s timer is
    read, so the compaction is charged to `apply_s`.
  * `ckpt_d_seconds` is sourced from `_ingest_checkpoint_policy`, which never
    sees this call, so the trace reports the commit as taking no checkpoint.

That combination is exactly the residual's shape: work-independent,
graph-size-driven, growing, landing in the intercept. #260's own verdict comment
named the lease span as a candidate; this measures it.

WHAT IS MEASURED. `try_acquire` and `release` are wrapped to count 0 -> 1 opens
and time 1 -> 0 drops across a real bounded `_run_ingestion`, with
`MINIGRAF_INGEST_TRACE_PATH` set so drop time can be stated as a share of the
same `apply_s` the #260 fit was built on.

WHY `release()` IS TIMED WHOLE. At 1 -> 0 the handle is freed before `release()`
returns (see its own comment about the strong reference in that frame), so the
Rust Drop checkpoint is inside the timed span. Timing anything narrower would
miss it.

SELF-ISOLATING: creates its own tempdir and points MINIGRAF_GRAPH_PATH,
MINIGRAF_INDEX_PATH and MINIGRAF_INGEST_TRACE_PATH at it, so it never touches
memory.graph.

INSTANCE-LEVEL MONKEYPATCHING (#272): the wrappers are attached to the
`_lease_manager` INSTANCE, which #272 records as a session-wide booby trap in
the test suite. Safe here only because this is a standalone process that exits
when the run ends. Do not copy the pattern into tests.

    .venv/bin/python evals/at_scale/probe_lease_drop_cost.py \
        --repo /path/to/repo --ref <sha> [--out results/280-lease-drop-cost.json]

Run with .venv/bin/python -- bare python on the development machine carries
minigraf 1.1.1 against this project's >=1.2.3 floor (#260's methodological
warning).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

_TMPDIR = tempfile.mkdtemp(prefix="probe260lease-")
os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(_TMPDIR, "m.graph")
os.environ["MINIGRAF_INDEX_PATH"] = os.path.join(_TMPDIR, "m.fts.sqlite3")
TRACE_PATH = os.path.join(_TMPDIR, "trace.jsonl")
os.environ["MINIGRAF_INGEST_TRACE_PATH"] = TRACE_PATH

import mcp_server as m  # noqa: E402 -- must follow the env setup above

T0 = time.perf_counter()
opens: list = []   # t_since_start of each 0 -> 1 open
drops: list = []   # [t_since_start, seconds] of each 1 -> 0 drop


def install_counters() -> None:
    orig_acquire = m._lease_manager.try_acquire
    orig_release = m._lease_manager.release

    def try_acquire(path=None):
        was_idle = m._lease_manager.lease_count == 0
        handle = orig_acquire(path)
        if handle is not None and was_idle:
            opens.append(time.perf_counter() - T0)
        return handle

    def release():
        # Read the count BEFORE releasing: after the call it is already
        # decremented and 1 -> 0 is indistinguishable from 2 -> 1.
        will_drop = m._lease_manager.lease_count == 1
        t = time.perf_counter()
        orig_release()
        elapsed = time.perf_counter() - t
        if will_drop:
            drops.append([time.perf_counter() - T0, elapsed])

    m._lease_manager.try_acquire = try_acquire
    m._lease_manager.release = release


def summarize(wall: float) -> dict:
    records = []
    if os.path.exists(TRACE_PATH):
        with open(TRACE_PATH) as f:
            records = [json.loads(line) for line in f if line.strip()]

    n = len(records)
    apply_total = sum(r["apply_s"] for r in records)
    ckpt_total = sum(r["ckpt_d_seconds"] for r in records)
    durations = [d for _, d in drops]
    drop_total = sum(durations)

    thirds = {}
    if len(durations) >= 3:
        k = len(durations) // 3
        first, mid, last = durations[:k], durations[k:2 * k], durations[2 * k:]
        thirds = {
            "first_mean_s": statistics.fmean(first),
            "middle_mean_s": statistics.fmean(mid),
            "last_mean_s": statistics.fmean(last),
            "growth": statistics.fmean(last) / statistics.fmean(first),
        }

    return {
        "issue": 260,
        "wall_s": wall,
        "commits_traced": n,
        "handle_opens": len(opens),
        "handle_drops": len(drops),
        "opens_per_commit": len(opens) / n if n else None,
        "apply_total_s": apply_total,
        "apply_share_of_wall": apply_total / wall if wall else None,
        "explicit_ckpt_total_s": ckpt_total,
        "explicit_ckpt_share_of_apply": ckpt_total / apply_total if apply_total else None,
        "drop_total_s": drop_total,
        "drop_share_of_wall": drop_total / wall if wall else None,
        "drop_share_of_apply": drop_total / apply_total if apply_total else None,
        "drop_thirds": thirds,
        "drops": drops,
        "opens": opens,
    }


def report(result: dict) -> None:
    print(f"wall                 {result['wall_s']:8.1f} s")
    print(f"commits traced       {result['commits_traced']:8d}")
    print(f"handle opens         {result['handle_opens']:8d}   "
          f"({result['opens_per_commit']:.2f} per commit)")
    print(f"handle drops         {result['handle_drops']:8d}")
    print(f"sum apply_s          {result['apply_total_s']:8.1f} s  "
          f"({result['apply_share_of_wall']:.1%} of wall)")
    print(f"sum ckpt_d_seconds   {result['explicit_ckpt_total_s']:8.1f} s  "
          f"({result['explicit_ckpt_share_of_apply']:.1%} of apply_s, explicit only)")
    print(f"sum drop time        {result['drop_total_s']:8.1f} s  "
          f"({result['drop_share_of_wall']:.1%} of wall, "
          f"{result['drop_share_of_apply']:.1%} of apply_s)")
    t = result["drop_thirds"]
    if t:
        print(f"drop mean by third   {t['first_mean_s']*1000:.1f} -> "
              f"{t['middle_mean_s']*1000:.1f} -> {t['last_mean_s']*1000:.1f} ms   "
              f"growth {t['growth']:.2f}x")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="Repository whose history is ingested.")
    p.add_argument("--ref", required=True, help="Ref/SHA bounding the slice.")
    p.add_argument("--out", default=None, help="Write the result JSON here.")
    args = p.parse_args()

    install_counters()
    t = time.perf_counter()
    asyncio.run(m._run_ingestion(args.repo, args.ref))
    result = summarize(time.perf_counter() - t)

    report(result)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}")
    print(f"graph dir (not cleaned up, inspect or rm): {_TMPDIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
