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
