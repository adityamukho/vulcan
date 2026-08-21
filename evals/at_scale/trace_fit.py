"""#260: does per-commit ingestion cost grow with history length once per-commit
work size is controlled for?

Pure functions over a per-commit trace (mcp_server._IngestTrace's JSONL). No
I/O, no DB -- so every pre-registered decision here is testable in
milliseconds against synthetic traces rather than behind a 30-minute run.

THE CONSTANTS BELOW ARE PRE-REGISTERED. They were fixed in
docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md before
any trace existed. Do NOT adjust them in response to a result. If a run shows
they were badly chosen, FORK this experiment -- re-baselining a pre-registered
experiment re-evaluates its predictions against a different experiment while
still printing a verdict, which is why probe_ident_collision_census.py's
PREDICTIONS block is frozen rather than updated.

The model, within each third of processed order:

    apply_s = a + b * W        W = idents_considered

Two parameters, because the hypotheses have different shapes and a
zero-intercept "cost per unit work" ratio cannot separate them:

  a grows, b flat  -> per-commit FIXED cost grows with graph size
                      (checkpointing is the prime suspect: _CheckpointPolicy
                      documents db.checkpoint() as O(graph size), ~5.1 ms/MB)
  b grows          -> cost PER UNIT WORK grows with graph size
  both flat        -> the growth is input-driven; #260 is confounded

The fit is identifiable only because #222's converging streams put small-work
(fwd) and large-work (rev) commits at the SAME graph size inside every window.
A single-stream walk would make W and graph size collinear and no fit could
separate a from b -- which is why fit_line returns None rather than a number
when a group has no variance in W.
"""

from __future__ import annotations

from typing import Optional

#: The frozen work metric. See _trace_work_counters for its arithmetic.
W_KEY = "idents_considered"

#: Both ratios strictly below this -> CONFOUNDED.
CLOSE_BELOW = 1.5

#: Either ratio at or above this -> REAL. Matches the threshold
#: bench_introduced_by_query_cost.py fixed for #239 before any data existed.
REAL_AT = 2.0

#: The positive control: mean per-checkpoint duration must grow at least this
#: much, or the method has failed open and the run is VOID.
CONTROL_MIN_GROWTH = 2.0

#: Fewer checkpoints than this in either group makes the control unevaluable.
CONTROL_MIN_CHECKPOINTS = 5

#: Fewer records than this in a group makes its fit untrustworthy.
MIN_POINTS_PER_GROUP = 30


def split_thirds(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Three equal-count groups in emission (processed) order.

    Not equal spans of wall clock and not equal spans of `pos`. Any remainder
    goes to the MIDDLE group, so the first and last stay the same size -- those
    two are what the verdict compares, and an uneven pair would bias the ratio.
    """
    n = len(records)
    size = n // 3
    if size == 0:
        return records, [], []
    return records[:size], records[size:n - size], records[n - size:]


def fit_line(xs: list[float], ys: list[float]) -> Optional[dict]:
    """OLS fit of y = a + b*x. None when the fit is not identifiable.

    Two None cases, both deliberate:
      - fewer than MIN_POINTS_PER_GROUP points;
      - zero variance in x, where a and b cannot be separated at all. Returning
        a number there would invent an intercept out of nothing.
    """
    n = len(xs)
    if n < MIN_POINTS_PER_GROUP or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = mean_y - b * mean_x
    sst = sum((y - mean_y) ** 2 for y in ys)
    ssr = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return {
        "a": a,
        "b": b,
        "r2": (1.0 - ssr / sst) if sst > 0 else 0.0,
        "n": n,
    }


def growth_ratio(first: Optional[float], last: Optional[float]) -> Optional[float]:
    """last/first, or None when that is not a meaningful number.

    A non-positive denominator is None rather than a ratio: an OLS intercept
    can legitimately come out negative, and a ratio taken across zero flips
    sign and reads as a small number -- reporting "flat" for a parameter that
    is nothing of the kind.
    """
    if first is None or last is None:
        return None
    if first <= 0.0:
        return None
    return last / first


def control_gate(first: list[dict], last: list[dict]) -> dict:
    """Does mean per-checkpoint duration grow from the first third to the last?

    Checkpoint cost is documented O(graph size), so this MUST grow. A method
    that cannot detect growth here cannot be trusted when it reports flatness
    anywhere else -- it has failed open, and a clean-looking result from a
    broken measurement is this project's most repeated failure mode.

    Gates on the per-checkpoint MEAN, never the total. The duty policy holds
    aggregate checkpointing to a fixed FRACTION of wall clock as the graph
    grows, so the total is designed not to grow while each checkpoint still
    does; gating on the total would fail on healthy behaviour.

    CONTROL_MIN_CHECKPOINTS is checked against EACH group individually, not
    their combined count: a "mean" drawn from a single checkpoint carries
    none of the averaging the floor exists to provide, so a group short on
    checkpoints must report unevaluable on its own rather than being
    rescued by the other group's count.
    """
    def mean(group: list[dict]) -> tuple[Optional[float], int]:
        count = sum(int(r.get("ckpt_d_count", 0)) for r in group)
        seconds = sum(float(r.get("ckpt_d_seconds", 0.0)) for r in group)
        if count < CONTROL_MIN_CHECKPOINTS:
            return None, count
        return seconds / count, count

    mean_first, n_first = mean(first)
    mean_last, n_last = mean(last)
    growth = growth_ratio(mean_first, mean_last)

    if mean_first is None or mean_last is None:
        reason = (
            f"unevaluable: need >= {CONTROL_MIN_CHECKPOINTS} checkpoints per "
            f"group, saw {n_first} (first) and {n_last} (last)"
        )
        passed = False
    elif growth is None:
        reason = "unevaluable: first-group mean per-checkpoint duration is not positive"
        passed = False
    elif growth < CONTROL_MIN_GROWTH:
        reason = (
            f"FAILED OPEN: mean per-checkpoint duration grew {growth:.2f}x, "
            f"below the {CONTROL_MIN_GROWTH}x this method must be able to see. "
            f"The run is VOID, not flat."
        )
        passed = False
    else:
        reason = f"passed: mean per-checkpoint duration grew {growth:.2f}x"
        passed = True

    return {
        "passed": passed,
        "growth": growth,
        "mean_first": mean_first,
        "mean_last": mean_last,
        "n_first": n_first,
        "n_last": n_last,
        "reason": reason,
    }


def verdict(a_ratio: Optional[float], b_ratio: Optional[float]) -> tuple[str, str]:
    """The pre-registered verdict on (a_last/a_first, b_last/b_first).

    REAL is checked FIRST because it is a disjunction: either parameter growing
    is enough, so a=2.5 with b=1.1 is REAL and not a mixed case.

    An undefined ratio reads INCONCLUSIVE, never CONFOUNDED. A fit that could
    not be read is not evidence of flatness, and letting it argue for closing
    #260 would be the same fail-open error the control gate exists to prevent.
    """
    if a_ratio is None or b_ratio is None:
        missing = "a" if a_ratio is None else "b"
        return "INCONCLUSIVE", (
            f"{missing}_ratio is undefined -- the fit could not be read, which "
            f"is not evidence of flatness"
        )
    grew = [name for name, r in (("a", a_ratio), ("b", b_ratio)) if r >= REAL_AT]
    if grew:
        return "REAL", (
            f"{' and '.join(grew)} grew >= {REAL_AT}x "
            f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
        )
    if a_ratio < CLOSE_BELOW and b_ratio < CLOSE_BELOW:
        return "CONFOUNDED", (
            f"both parameters below {CLOSE_BELOW}x "
            f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
        )
    return "INCONCLUSIVE", (
        f"in the {CLOSE_BELOW}-{REAL_AT}x band "
        f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
    )


def analyse(records: list[dict]) -> dict:
    """The whole pipeline: split, fit, gate, verdict.

    The control gate is applied LAST and OVERRIDES the fit. A run whose control
    failed reports VOID and does not get to report CONFOUNDED -- see
    control_gate for why that ordering is the point.
    """
    first, middle, last = split_thirds(records)
    fits = {}
    for label, group in (("first", first), ("middle", middle), ("last", last)):
        xs = [float(r[W_KEY]) for r in group]
        ys = [float(r["apply_s"]) for r in group]
        fits[label] = fit_line(xs, ys)

    def param(label: str, key: str) -> Optional[float]:
        f = fits.get(label)
        return None if f is None else f[key]

    a_ratio = growth_ratio(param("first", "a"), param("last", "a"))
    b_ratio = growth_ratio(param("first", "b"), param("last", "b"))
    gate = control_gate(first, last)
    name, why = verdict(a_ratio, b_ratio)
    if not gate["passed"]:
        name, why = "VOID", f"control gate did not pass: {gate['reason']}"

    return {
        "records": len(records),
        "group_sizes": {"first": len(first), "middle": len(middle), "last": len(last)},
        "fits": fits,
        "a_ratio": a_ratio,
        "b_ratio": b_ratio,
        "control_gate": gate,
        "verdict": name,
        "verdict_reason": why,
        "pre_registered": {
            "W_KEY": W_KEY,
            "CLOSE_BELOW": CLOSE_BELOW,
            "REAL_AT": REAL_AT,
            "CONTROL_MIN_GROWTH": CONTROL_MIN_GROWTH,
            "CONTROL_MIN_CHECKPOINTS": CONTROL_MIN_CHECKPOINTS,
            "MIN_POINTS_PER_GROUP": MIN_POINTS_PER_GROUP,
        },
    }
