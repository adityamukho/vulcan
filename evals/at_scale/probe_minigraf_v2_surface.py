# evals/at_scale/probe_minigraf_v2_surface.py
"""#284 upgrade-surface probe: what minigraf 2.0.0 actually changes for us.

Read-only. Run it TWICE -- once per interpreter -- and diff the two JSON
outputs. It answers four questions the #284 issue body asked to be MEASURED
rather than derived from the release notes, and it exists because three of
the four answers were not what reading the notes predicted.

    .venv/bin/python evals/at_scale/probe_minigraf_v2_surface.py \
        --out evals/at_scale/results/284-v2-surface-1.2.3.json
    /path/to/venv20/bin/python evals/at_scale/probe_minigraf_v2_surface.py \
        --out evals/at_scale/results/284-v2-surface-2.0.0.json

There is no committed 2.0.0 environment: pyproject caps minigraf <2.0.0 (#286)
precisely so CI cannot drift onto it, so the 2.0.0 run needs a throwaway venv
built by hand (`pip install -e ".[dev,git-ingestion]"` then a forced
`pip install minigraf==2.0.0`, which will warn about the cap -- that warning is
the cap working, not a problem).

WHAT EACH SECTION MEASURES, AND WHAT IT PROVED (2026-08-31, 1.2.3 vs 2.0.0)

`error_strings` -- provokes each condition the repo string-matches on and
records the real message, then runs mcp_server's OWN predicates against the
resulting exception rather than against a transcription of it.
  * _is_lock_error SURVIVES. Both "locked" and "already open in this process"
    are still present under the new [STG-026]/[STG-025] prefixes, and the
    predicate lowercases and substring-matches, so the [CODE] prefix is inert.
  * _stale_lock_holder_pid BREAKS: "holder PID: N" is gone from both messages
    and it returns None. Call sites mcp_server.py:3208 and :11710.
  NOT COVERED: stderr_capture.py's page_out_of_bounds,
  serde_deserialization_error and stream_all_entries_expected_leaf_page.
  Those are internal corruption states with no cheap trigger. They are safe
  from the PREFIX by construction -- scan_ingestion_stderr uses an unanchored
  pattern.search(line) -- but a WORDING change is not ruled out for them, and
  wording changes are real: "Retract argument must be a vector" gained "of
  facts". Treat those three as unverified, not as cleared.

`lock_timing` -- the cost of one contended open, and what mcp_server's
5-attempt/0.05s-doubling budget really spends against a lock held throughout.
Confirms the issue's ~375ms prediction (376.1ms measured) and a 3.51x budget
overrun (750ms designed -> 2631ms actual).

`hook_path` -- the section that INVERTED the issue's conclusion, and the
reason to keep this probe rather than delete it with the upgrade. Sweeps how
long a foreign process holds the graph, then runs the real
mcp_server.db_lease(). A failure here is a SILENTLY discarded auto-memory
write, because hooks/finalize_hook.py is `except Exception: pass`. 2.0.0 is
better on BOTH axes -- contention tolerance rises from ~0.5s to ~2.5s AND
latency drops where it already worked (227ms vs 352ms at a 0.2s hold) --
because the 375ms is an adaptive wait polling 5->50ms that returns the moment
the lock frees, whereas our loop sleeps in coarse 50/100/200/400ms blocks and
only rechecks at those boundaries. The 376ms figure appears in full ONLY when
the lock is never released. This is why #284 must NOT shrink
_LOCK_RETRY_MAX/_LOCK_RETRY_BASE to reclaim the 3.51x.

`precheck` -- whether #108's "don't race for the lock, decline" pre-check
still works. It does not. _live_lock_holder_pid reads the .graph.lock sidecar
that upstream #317 deleted, so under 2.0.0 it returns None while another
process demonstrably holds the graph, and ingestion silently goes back to
racing. This was NOT in #284's scope and is the largest piece of the upgrade.

EVERY SECTION CARRIES A POSITIVE CONTROL. A probe that reports "not held" or
"no error" because its own setup silently failed would otherwise read as a
clean result -- the failure mode this repo has been bitten by before. The
controls are recorded in the JSON as `control_*` keys; a consumer MUST treat a
false control as "this run measured nothing", never as a negative finding.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any, Callable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

HOLD_SWEEP_SECONDS = (0.2, 0.5, 0.8, 1.5, 2.5, 3.5)
SINGLE_OPEN_SAMPLES = 5


def _minigraf_version() -> str:
    import importlib.metadata as md

    return md.version("minigraf")


def _hold_graph(graph: str, seconds: float) -> subprocess.Popen:
    """Start a subprocess that opens `graph` and holds it for `seconds`.

    Returns once the child has confirmed it holds the handle, so the caller
    never measures against a child that has not opened yet.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import time
                from minigraf import MiniGrafDb
                db = MiniGrafDb.open({graph!r})
                print("HELD", flush=True)
                time.sleep({seconds})
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if line != "HELD":
        child.kill()
        child.wait()
        raise RuntimeError(f"holder subprocess never took the handle (said {line!r})")
    return child


def _fresh_graph() -> str:
    from minigraf import MiniGrafDb

    path = os.path.join(tempfile.mkdtemp(), "probe.graph")
    MiniGrafDb.open(path).checkpoint()  # materialise, then drop
    return path


# ---------------------------------------------------------------------------
# Section 1: error strings
# ---------------------------------------------------------------------------

def section_error_strings() -> dict[str, Any]:
    from minigraf import MiniGrafDb
    import mcp_server

    out: dict[str, Any] = {"conditions": {}}

    def record(name: str, fn: Callable[[], Any], predicates: bool = False) -> None:
        entry: dict[str, Any]
        try:
            fn()
            entry = {"raised": False, "type": None, "text": None}
        except Exception as exc:  # noqa: BLE001 -- recording whatever comes out is the point
            entry = {"raised": True, "type": type(exc).__name__, "text": str(exc)}
            if predicates:
                # Run the LIVE predicates against the real exception object.
                entry["_is_lock_error"] = mcp_server._is_lock_error(exc)
                # _stale_lock_holder_pid was DELETED by #284 item 5. The
                # regex it ran is inlined here so this probe keeps reporting
                # whether a holder PID is still scrapeable from the message
                # -- the measurement that justified deleting it.
                match = re.search(r"holder PID:\s*(\d+)", str(exc))
                entry["holder_pid_scrapeable"] = int(match.group(1)) if match else None
        out["conditions"][name] = entry

    path = _fresh_graph()

    def with_open_handle() -> None:
        """Scope the handle to this call so it is released on return.

        The cross-process section below needs the graph genuinely free; a
        handle still live in THIS process would make the child's open fail
        for the wrong reason and quietly invalidate that measurement.
        """
        db = MiniGrafDb.open(path)
        record("same_process_second_open", lambda: MiniGrafDb.open(path), predicates=True)
        record("unclosed_vector", lambda: db.execute("(query [:find ?x :where [?e :a ?x])"))
        record("retract_non_vector", lambda: db.execute("(retract :not-a-vector)"))
        record(
            "invalid_date",
            lambda: db.execute('(transact {:valid-from "1000000000000"} [[:a/b :c "d"]])'),
        )
        record(
            "trailing_input",
            lambda: db.execute("(query [:find ?x :where [?e :a ?x]]) trailing"),
        )

    with_open_handle()

    child = _hold_graph(path, 20)
    try:
        record("cross_process_open", lambda: MiniGrafDb.open(path), predicates=True)
        # Positive control: the cross-process condition is only meaningful if
        # the holder really holds it, which the record() above just proved by
        # raising. Surface it explicitly so a consumer need not infer it.
        out["control_cross_process_really_held"] = out["conditions"][
            "cross_process_open"
        ]["raised"]
    finally:
        child.kill()
        child.wait()

    out["sidecar_present_while_held"] = os.path.exists(path + ".lock")
    return out


# ---------------------------------------------------------------------------
# Section 2: contended open cost and the retry budget
# ---------------------------------------------------------------------------

def section_lock_timing() -> dict[str, Any]:
    from minigraf import MiniGrafDb
    import mcp_server

    path = _fresh_graph()
    child = _hold_graph(path, 60)
    out: dict[str, Any] = {}
    try:
        samples = []
        for _ in range(SINGLE_OPEN_SAMPLES):
            t0 = time.perf_counter()
            try:
                MiniGrafDb.open(path)
                samples.append({"blocked": False, "seconds": time.perf_counter() - t0})
                break  # acquiring means the holder died; stop sampling
            except Exception:  # noqa: BLE001
                samples.append({"blocked": True, "seconds": time.perf_counter() - t0})
        out["single_open_samples"] = samples
        out["single_open_mean_seconds"] = sum(s["seconds"] for s in samples) / len(samples)
        # Positive control: every sample must have been blocked, else the
        # holder was not holding and the mean is meaningless.
        out["control_all_samples_blocked"] = all(s["blocked"] for s in samples)

        # Replay mcp_server's real budget shape against a lock held throughout.
        t0 = time.perf_counter()
        attempts = 0
        slept = 0.0
        delay = mcp_server._LOCK_RETRY_BASE
        for attempt in range(mcp_server._LOCK_RETRY_MAX):
            attempts += 1
            try:
                MiniGrafDb.open(path)
                break
            except Exception:  # noqa: BLE001
                if attempt < mcp_server._LOCK_RETRY_MAX - 1:
                    time.sleep(delay)
                    slept += delay
                    delay *= 2
        total = time.perf_counter() - t0
        out["budget"] = {
            "lock_retry_max": mcp_server._LOCK_RETRY_MAX,
            "lock_retry_base": mcp_server._LOCK_RETRY_BASE,
            "real_open_calls": attempts,
            "designed_sleep_seconds": slept,
            "actual_wall_clock_seconds": total,
            "overrun_factor": (total / slept) if slept else None,
        }
    finally:
        child.kill()
        child.wait()
    return out


# ---------------------------------------------------------------------------
# Section 3: the real auto-memory hook lease path
# ---------------------------------------------------------------------------

def section_hook_path() -> dict[str, Any]:
    import mcp_server

    rows = []
    for hold in HOLD_SWEEP_SECONDS:
        graph = os.path.join(tempfile.mkdtemp(), "hook.graph")
        os.environ["MINIGRAF_GRAPH_PATH"] = graph
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)      # materialise
        mcp_server._reset_db_state()   # drop ours so the child can take it

        child = _hold_graph(graph, hold)
        t0 = time.perf_counter()
        try:
            with mcp_server.db_lease() as db:
                mcp_server._db_execute(db, "(query [:find ?e :where [?e :ident ?v]])")
            acquired = True
        except Exception:  # noqa: BLE001 -- exactly what finalize_hook swallows
            acquired = False
        elapsed = time.perf_counter() - t0
        child.wait()
        mcp_server._reset_db_state()

        rows.append(
            {
                "hold_seconds": hold,
                "lease_acquired": acquired,
                "elapsed_seconds": elapsed,
                # A failure here is not an error report -- finalize_hook.py
                # swallows it, so the turn's facts are silently discarded.
                "auto_memory_write": "kept" if acquired else "silently_lost",
            }
        )
    return {"sweep": rows}


# ---------------------------------------------------------------------------
# Section 4: the #108 pre-check
# ---------------------------------------------------------------------------

def section_precheck() -> dict[str, Any]:
    from minigraf import MiniGrafDb
    import mcp_server

    import json as _json

    path = _fresh_graph()
    child = _hold_graph(path, 20)
    try:
        # Positive control: prove the holder REALLY holds it, independently of
        # anything under test. Without this, a None answer below is
        # indistinguishable from "nothing was holding the graph".
        try:
            MiniGrafDb.open(path)
            control_held = False
        except Exception:  # noqa: BLE001
            control_held = True

        # The OLD mechanism, inlined because #284 item 5 deleted it: read the
        # `.graph.lock` sidecar. This is the measurement that condemned it --
        # on 2.0.0 it cannot see a holder that demonstrably holds the graph.
        sidecar_pid = None
        try:
            with open(path + ".lock") as f:
                raw = f.read().strip()
            sidecar_pid = int(raw) if raw.isdigit() else None
        except (OSError, ValueError):
            sidecar_pid = None

        # The NEW mechanism: our own ownership hint. Version-independent by
        # construction, which is the whole point -- it does not read
        # minigraf's lock, so kernel locking cannot silence it.
        hint_path = mcp_server._owner_hint_path(path)
        with open(hint_path, "w") as f:
            _json.dump(
                {"pid": child.pid, "host": "some-other-host", "purpose": "ingestion"}, f
            )
        try:
            hint = mcp_server._graph_owner_hint(path)
        finally:
            os.remove(hint_path)
        expected_pid = child.pid
    finally:
        child.kill()
        child.wait()

    return {
        "control_open_refused_while_held": control_held,
        "holder_subprocess_pid": expected_pid,
        # Old, deleted mechanism -- kept as the historical finding.
        "sidecar_holder_pid": sidecar_pid,
        "sidecar_precheck_is_silent_noop": control_held and sidecar_pid is None,
        # New mechanism -- must work identically on both versions.
        "owner_hint_detects_holder": hint is not None,
        "owner_hint_pid": hint.get("pid") if hint else None,
    }


# ---------------------------------------------------------------------------
# Section 5: crash recovery, and whether _clear_stale_lock is load-bearing
# ---------------------------------------------------------------------------

def section_stale_recovery() -> dict[str, Any]:
    """Does a HARD-KILLED holder leave the graph unopenable?

    Load-bearing for #284 item 5's staging decision. mcp_server carries a
    stale-lock self-heal (_clear_stale_lock, driven from _open_for_lease); the
    question is whether deleting it is safe on the version we actually run.

    Reading 1.2.3's error text ("If no other process is using this database,
    delete the lock file manually") suggests it is NOT safe. That reading is
    WRONG, which is why this is measured: 1.2.3 leaves the sidecar on disk
    after a SIGKILL but reopens successfully anyway, because it checks the
    recorded PID's liveness itself. The self-heal is redundant on BOTH
    versions. Do not re-derive this from the error message.
    """
    import signal

    from minigraf import MiniGrafDb

    path = _fresh_graph()
    child = _hold_graph(path, 30)

    # Positive control: while genuinely held, an open MUST fail. Without this,
    # "reopen succeeded" could just mean the holder never held it.
    try:
        MiniGrafDb.open(path)
        control_held = False
    except Exception:  # noqa: BLE001
        control_held = True

    os.kill(child.pid, signal.SIGKILL)  # hard kill: no cleanup path runs
    child.wait()
    time.sleep(0.5)

    sidecar_left = os.path.exists(path + ".lock")
    try:
        MiniGrafDb.open(path)
        reopened, error = True, None
    except Exception as exc:  # noqa: BLE001
        reopened, error = False, str(exc)

    return {
        "control_open_refused_while_held": control_held,
        "sidecar_left_after_sigkill": sidecar_left,
        "reopen_after_sigkill_succeeds": reopened,
        "reopen_error": error,
        # True would mean the self-heal is genuinely required on this version.
        "clear_stale_lock_is_required": control_held and not reopened,
    }


SECTIONS: dict[str, Callable[[], dict[str, Any]]] = {
    "error_strings": section_error_strings,
    "lock_timing": section_lock_timing,
    "hook_path": section_hook_path,
    "precheck": section_precheck,
    "stale_recovery": section_stale_recovery,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument(
        "--section",
        action="append",
        choices=sorted(SECTIONS),
        help="run only these sections (repeatable; default: all)",
    )
    args = ap.parse_args()

    names = args.section or sorted(SECTIONS)
    result: dict[str, Any] = {
        "minigraf_version": _minigraf_version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "sections": {},
    }
    for name in names:
        result["sections"][name] = SECTIONS[name]()

    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
