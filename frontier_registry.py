"""Frontier/interval registry + shared-gap allocator for #222 phase 1.

Represents ingestion progress as a small set of disjoint, tagged intervals
over a fixed topological commit linearization, rather than a single scalar
watermark -- the foundation phase 2 builds concurrent forward-truth /
reverse-bulk-fill streams on top of. See
docs/superpowers/specs/2026-07-24-frontier-registry-design.md.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

TAG_AUTHORITATIVE = "authoritative"
TAG_PROVISIONAL = "provisional"


@dataclass
class Interval:
    lo_pos: int
    hi_pos: int
    tag: str


def build_linearization(repo_path: str, branch: str = "HEAD") -> List[str]:
    """Full C0..branch commit hash list in fixed topological order (oldest first).

    --topo-order guarantees parent-before-child even when committer dates are
    non-monotonic (clock skew, rebases) -- plain chronological `git log`
    order does not.
    """
    result = subprocess.run(
        ["git", "log", "--topo-order", "--reverse", "--format=%H", branch],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.strip().splitlines() if line.strip()]


class FrontierAllocator:
    """In-memory shared-gap allocator over a fixed linearization.

    Holds at most two intervals: one anchored at position 0
    (tag=authoritative, grows upward via claim_low) and one anchored at the
    last position (tag=provisional, grows downward via claim_high). They are
    never merged into each other even once adjacent -- the boundary between
    them is the lineage-authority frontier later phases read.
    """

    def __init__(self, total_positions: int, intervals: Optional[List[Interval]] = None):
        self.total_positions = total_positions
        self._intervals: List[Interval] = list(intervals or [])

    @property
    def gap_lo(self) -> int:
        low = self._interval_covering(0)
        return low.hi_pos + 1 if low else 0

    @property
    def gap_hi(self) -> int:
        last = self.total_positions - 1
        high = self._interval_covering(last)
        return high.lo_pos - 1 if high else last

    def is_gap_empty(self) -> bool:
        return self.gap_lo > self.gap_hi

    def intervals(self) -> List[Interval]:
        return list(self._intervals)

    def _interval_covering(self, pos: int) -> Optional[Interval]:
        for iv in self._intervals:
            if iv.lo_pos <= pos <= iv.hi_pos:
                return iv
        return None

    def claim_low(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_lo
        self._extend(pos, tag=TAG_AUTHORITATIVE, from_low=True)
        return pos

    def claim_high(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_hi
        self._extend(pos, tag=TAG_PROVISIONAL, from_low=False)
        return pos

    def _adjacent_interval(self, pos: int, tag: str, from_low: bool) -> Optional[Interval]:
        """The same-tag interval this claim should grow: the one whose edge
        touches pos from the direction of growth.

        Deliberately NOT "the first interval covering the neighbour
        position" (#222 phase 2b1). Those differ once a side holds two
        intervals, which happens whenever a run reloads a persisted high
        interval that no longer reaches the last position -- new commits
        landed on HEAD since. There, claim_high() opens a second provisional
        interval near the top while the reloaded one sits lower, and picking
        the first *covering* interval can select the lower one and rewrite it
        to itself: gap_hi never moves, claim_high() returns the same position
        forever, and _reverse_bulk_fill_walk spins on it, re-parsing and
        re-fsyncing every iteration.
        """
        for iv in self._intervals:
            if iv.tag != tag:
                continue
            touches = (iv.hi_pos == pos - 1) if from_low else (iv.lo_pos == pos + 1)
            if touches:
                return iv
        return None

    def _coalesce(self, tag: str) -> None:
        """Merge same-tag intervals that overlap or touch, keeping intervals
        disjoint and sorted.

        Growing toward a stale interval eventually makes the two meet; left
        overlapping, _interval_covering's first-match lookup becomes
        order-dependent and gap_lo/gap_hi stop describing a coherent gap.
        Only same-tag intervals merge -- the authoritative/provisional
        boundary is the lineage frontier later phases read, and must survive
        the two sides becoming adjacent.
        """
        same = sorted((iv for iv in self._intervals if iv.tag == tag), key=lambda iv: iv.lo_pos)
        merged: List[Interval] = []
        for iv in same:
            if merged and iv.lo_pos <= merged[-1].hi_pos + 1:
                prev = merged[-1]
                merged[-1] = Interval(prev.lo_pos, max(prev.hi_pos, iv.hi_pos), tag)
            else:
                merged.append(iv)
        others = [iv for iv in self._intervals if iv.tag != tag]
        self._intervals = sorted(others + merged, key=lambda iv: iv.lo_pos)

    def _extend(self, pos: int, tag: str, from_low: bool) -> None:
        target = self._adjacent_interval(pos, tag, from_low)
        if target is not None:
            idx = next(i for i, iv in enumerate(self._intervals) if iv is target)
            if from_low:
                self._intervals[idx] = Interval(target.lo_pos, pos, tag)
            else:
                self._intervals[idx] = Interval(pos, target.hi_pos, tag)
        else:
            self._intervals.append(Interval(pos, pos, tag))
        self._coalesce(tag)
