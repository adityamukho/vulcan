# evals/at_scale/probe_resume_census.py
"""#325: the commit census on a RESUMED graph -- the scenario nothing else runs.

WHY THE NIGHTLY'S OWN commit_census CANNOT SEE THIS. `commit_census` (#317)
runs inside `run_ingestion_benchmark`, which ingests once into a fresh graph.
#325 makes `_frontier_load` RETAIN a persisted high interval whose bounds
still resolve, `lo <= hi`, and whose stored `:pos-count` still matches its
span -- and a WRONGLY-retained interval means its positions are never
re-claimed, on any run, ever. A fresh-graph run has no interval to retain in
the first place, so it can never exercise the failure mode this issue
introduced. Every other at-scale detector reads a wrong retention clean too:
`fact_audit`'s two witnesses agree perfectly about a commit neither the graph
nor the index ever received; both `:introduced-by` checks (#287, #316) only
examine entities that EXIST; and `stderr_capture` has nothing to read, because
skipping a position that looks complete prints nothing.

WHAT THIS PROBE DOES. Ingests `<branch>~<truncate_by>` into a fresh graph,
then ingests `<branch>` into the SAME graph -- the resume that can trigger
retention -- then hands the three resulting counts to the existing
`collect_commit_census`, reused VERBATIM. Reusing it rather than
reimplementing the comparison is deliberate: a second copy of "count three
things and diff them" is exactly how this probe and the nightly gate could
drift into silently counting different things.

WALK_CLAIMED IS NOT A MANUAL RESET, AND ITS OWN FORMULA IS ALREADY
ESTABLISHED. `_run_ingestion` seeds `_ingest_progress["processed"]` with its
own freshly recomputed `prior_ingested` (`_count_commit_entities(db)`, run
again at the TOP of every call -- see `_load_ingestion_preload_state`) and
then increments `processed` for every position retired this run, including
ones already inside that seed. `handle_minigraf_ingest_status` already derives
`processed_this_run = _ingest_progress["processed"] -
_ingest_progress.get("prior_ingested", 0)` for exactly this reason (issue
#85); this probe reuses that same formula rather than re-deriving it, and
rather than the earlier draft's `_ingest_progress["processed"] = 0` reset
before the resume call -- which does nothing, because `_run_ingestion`
overwrites `processed` back to its own `prior_ingested` immediately after
setting `_ingest_progress["status"] = "running"`, before a single commit is
walked. That draft's `this_run` was silently the CUMULATIVE total, not the
run's own delta, and handing it to `walk_claimed = prior + this_run` would
have double-counted the overlap.
`_ingest_progress["processed"]` after the resume call already equals
`prior_ingested + processed_this_run` by construction, so `walk_claimed` is
just that field, read once, after the resume completes.

WHY THE PROBE'S OWN `ok` IS `repo_vs_graph`, NOT collect_commit_census's --
A CONTROLLER RULING, not this file's own design choice. `commit_census`'s `ok`
gates on `ident_collisions`, `walk_vs_graph` (always) and `repo_vs_walk` (when
complete). `repo_vs_walk` compares the repo against `walk_claimed`, and on a
resume that skips already-completed positions (the exact #325 fast path)
`walk_claimed` legitimately undercounts "commits this run attempted" relative
to what `repo_vs_walk` was written to mean, for a perfectly healthy run --
that field was designed and measured against a fresh-graph run, which never
skips. `repo_vs_graph` asks the only question this probe exists to answer:
after a resume, does the graph hold every commit the repo has? It is
insensitive to how many positions were retired this run, by construction --
see `resume_ok`.

THE REF IS THE RESOLVED BRANCH, NEVER "HEAD". `_run_ingestion`'s own
`repo_total` was hardcoded to `HEAD` while ingestion took a `branch` argument,
live in the very run that measured #317 -- the at-scale nightly still invokes
`run_ingestion_benchmark` with the literal string `--branch HEAD`
(`.github/workflows/at-scale-benchmark-nightly.yml`), which is a separate,
untouched defect. This probe's `branch` parameter has no default and is never
substituted with `"HEAD"`; the nightly step added alongside this probe
resolves the checked-out branch name with `git rev-parse --abbrev-ref HEAD`
and passes THAT string.

STATUS KEY VERIFIED, NOT ASSUMED. `_ingest_progress["status"]` is the field
`handle_minigraf_ingest_status` itself reads (mcp_server.py) and the one
`_run_ingestion` sets to `"running"`, `"complete"`, `"stopped"`, `"error"` or
`"skipped"` -- confirmed by reading `_run_ingestion` and
`handle_minigraf_ingest_status` directly rather than assumed from the brief.

See results/325-resume-census.json for the measured baseline and
evals/at_scale/benchmark.md for the section documenting it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # noqa: E402

from evals.at_scale.commit_census import collect_commit_census  # noqa: E402

__all__ = ["run_resume_census", "resume_ok", "main"]

# The clean, un-contaminated baseline `handle_minigraf_ingest_git` writes
# before starting a run (mcp_server.py). Mirrored here rather than imported
# because it is a dict LITERAL there, not a named constant -- and because this
# probe calls `_run_ingestion` directly, bypassing the handler that normally
# writes it. Without this reset, `_ingest_progress` is a bare module global
# that outlives one `run_resume_census` call: a second call in the same
# process (this file's own two-test suite runs both in one pytest session)
# would start from whatever the FIRST call's run left behind -- e.g. an empty
# second repo's failed `_run_ingestion` calls touch `processed` and
# `prior_ingested` not at all, so a prior test's real counts would leak
# straight through as this run's numbers.
_CLEAN_INGEST_PROGRESS: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    "phase": None, "positions_skipped": 0,
}


def resume_ok(census: Dict[str, Any]) -> bool:
    """The probe's own verdict -- deliberately NOT `census["ok"]`.

    See the module docstring's "WHY THE PROBE'S OWN `ok`" section for why
    `collect_commit_census`'s gate (ident collisions, `walk_vs_graph` always,
    `repo_vs_walk` when complete) is the wrong instrument on a resumed graph.
    `repo_vs_graph` is the one delta that does not route through
    `walk_claimed` at all, so it reads identically whether this run's resume
    skipped every already-ingested position or re-walked all of them --
    exactly the property a check of #325's retention predicate needs.

    A census_error routes BOTH `repo_commits` and `graph_commit_entities` to
    the same zero default inside `collect_commit_census` (see its own
    docstring: "the collection fails into `census_error`, never into a
    zero" -- meaning the *comparison* never mistakes it for an empty repo,
    not that the raw counts stay non-zero). So `repo_vs_graph` reads 0 -- and
    this reads `True` -- on a run whose collection failed outright, same as
    on a genuinely empty repo. That is intentional here, not accidental: it
    mirrors `commit_census`'s own accepted empty-repo exemption, the raw
    `census_error` string ships in the result for a human to see either way,
    and `main()` fails the run unconditionally on `census_error` regardless
    of `--fail-on-mismatch` -- the same two-axis split
    `probe_ident_collision_new_history.py` uses between `measurement_invalid`
    (always fails) and `--fail-on-collision` (gates the finding).
    """
    return census["repo_vs_graph"] == 0


async def run_resume_census(
    repo_path: str, branch: str, graph_path: str, truncate_by: int
) -> Dict[str, Any]:
    """Ingest `<branch>~<truncate_by>`, resume to `<branch>` in the SAME
    graph, then census the result. See the module docstring for the full
    design; this is the collection half, mirroring
    `collect_commit_census`'s own split between collection and comparison.

    Single-handle invariant: `_reset_db_state()` runs first, unconditionally,
    so a leaked lease from an earlier call in this process (another test in
    this file, or a prior probe run in a long-lived caller) is released
    before this one opens its own.
    """
    mcp_server._reset_db_state()
    mcp_server.open_db(graph_path)
    mcp_server._ingest_progress = dict(_CLEAN_INGEST_PROGRESS)

    truncated_ref = f"{branch}~{truncate_by}" if truncate_by else branch
    try:
        await mcp_server._run_ingestion(repo_path, truncated_ref)
    except Exception:  # noqa: BLE001 -- _run_ingestion itself swallows
        # Exception internally and reports failure through
        # _ingest_progress["status"] / ["error"]; this only guards the
        # BaseException-rooted control-flow paths (asyncio.CancelledError)
        # its own `except Exception` does not cover, so a truncated ref that
        # cannot resolve (e.g. an empty repo has no `~N`) still lets the
        # resume attempt run and collect_commit_census report the failure
        # rather than this coroutine crashing outright.
        pass

    try:
        await mcp_server._run_ingestion(repo_path, branch)
    except Exception:  # noqa: BLE001 -- see above
        pass

    walk_claimed = mcp_server._ingest_progress["processed"]
    prior_ingested = mcp_server._ingest_progress.get("prior_ingested", 0)
    processed_this_run = walk_claimed - prior_ingested
    final_status = mcp_server._ingest_progress.get("status", "error")

    census = collect_commit_census(
        repo_path=repo_path,
        ref=branch,
        walk_claimed=walk_claimed,
        db=mcp_server.get_db(),
        final_status=final_status,
    )
    census["prior_ingested"] = prior_ingested
    census["processed_this_run"] = processed_this_run
    census["truncate_by"] = truncate_by
    # #326's skip fast path is what this whole probe exists to watch: a
    # position counted here without a matching write is exactly a retained
    # interval doing its job, and a resume that shows 0 despite a nonzero
    # processed_this_run is evidence the fast path did NOT engage for this
    # run (still `ok` if the graph is complete regardless, but worth seeing
    # rather than inferring from processed_this_run alone).
    census["positions_skipped_this_run"] = mcp_server._ingest_progress.get(
        "positions_skipped", 0
    )
    # Overrides collect_commit_census's own `ok` -- see resume_ok's docstring
    # for why that field is unsound on a resumed graph. `census["ok"]` (that
    # verdict) is kept alongside it under a distinct key rather than
    # discarded, so a reader can still see what the walk-based gate would
    # have said.
    census["commit_census_ok"] = census["ok"]
    census["ok"] = resume_ok(census)
    return census


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Census a RESUMED graph: ingest <branch>~<truncate-by>, then "
            "resume to <branch> in the same graph, then compare repo/walk/"
            "graph commit counts. The only at-scale check that can observe "
            "a wrongly-RETAINED frontier interval (#325) -- see the module "
            "docstring."
        )
    )
    ap.add_argument("--repo", required=True, help="Path to the repo to ingest.")
    ap.add_argument(
        "--branch", required=True,
        help="The RESOLVED branch name -- never 'HEAD'. See the module docstring.",
    )
    ap.add_argument("--graph", required=True, help="Graph path for the scratch resume graph.")
    ap.add_argument(
        "--truncate-by", type=int, required=True,
        help=(
            "Commits to hold back from the first ingestion (ingests "
            "<branch>~<truncate-by>, then resumes to <branch>). Large enough "
            "to exercise retention, small enough to stay affordable in a "
            "nightly -- see evals/at_scale/benchmark.md for the chosen value "
            "and its measured cost."
        ),
    )
    ap.add_argument("--out")
    ap.add_argument(
        "--fail-on-mismatch", action="store_true",
        help=(
            "Exit 1 if repo_vs_graph is nonzero -- a resume that lost a "
            "commit. OFF by default, matching probe_ident_collision_new_"
            "history.py's precedent: a finding must not be indistinguishable "
            "from an invalid run. A census_error fails the run regardless of "
            "this flag; see below."
        ),
    )
    args = ap.parse_args()

    result = asyncio.run(
        run_resume_census(args.repo, args.branch, args.graph, args.truncate_by)
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")

    # UNCONDITIONAL, like probe_ident_collision_new_history.py's
    # measurement_invalid: a census whose own collection failed must never
    # exit 0 just because nobody passed --fail-on-mismatch. This is the
    # counterpart to resume_ok's documented swallow -- the dict-level `ok`
    # reads True on a census_error (mirroring the empty-repo exemption), but
    # the CLI's exit code does not let that failure sit silently in a green
    # log.
    census_error: Optional[str] = result.get("census_error")
    if census_error is not None:
        print(f"\nCENSUS COLLECTION FAILED: {census_error}", file=sys.stderr)
        return 1
    if args.fail_on_mismatch and result["ok"] is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
