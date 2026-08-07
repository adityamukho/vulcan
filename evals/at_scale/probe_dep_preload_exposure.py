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
