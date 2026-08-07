# `:depends-on` preload exposure probe, and the benchmark poller fix

Date: 2026-08-07
Issues: #245 (measure), #242 (fix), refs #238, refs #222

## Purpose

Two independent changes, one branch.

**#245** asks whether `:depends-on` and `:pinned-commit` preload bounds — the
one quarter of #238 that its fix did not reach — expose enough real risk to
justify a fix. The issue deliberately makes no recommendation and asks for
exposure to be quantified first, the way #238 quantified its own inverted
positions. This spec builds that measurement. **It does not fix anything.**

**#242** is folded in because a full-history ingestion is a prerequisite for the
measurement, and today's benchmark harness cannot finish one: its poller starves
the ingestion it measures.

## Scope

In scope:

- `evals/at_scale/probe_dep_preload_exposure.py` — new exposure probe. Read-only
  with respect to any existing graph: it ingests into a scratch graph of its
  own and never writes to one it did not create.
- `evals/at_scale/run_ingestion_benchmark.py` — #242's poller fix.
- `evals/at_scale/benchmark.md` — a note that prior entries carry poller overhead.
- Tests for both, in `tests/test_at_scale_ingestion_benchmark.py` and a new
  probe test module.
- Issue comments recording the measured number and the `:pinned-commit` finding.

Out of scope, explicitly:

- Any change to `_preload_known_deps`, `_preload_pinned_commits`, or the fact
  model. #245's three options stay unbuilt until the number says which, if any,
  is warranted.
- Closing #238. It stays open until all four preload sites are resolved. Commit
  messages and the PR body use `Refs #238` and `Refs #245`, never a closing
  keyword — GitHub scans both, and on this project a *negated* "does not close
  #N" has still auto-closed an issue.
- Closing #245. The measurement is the deliverable; the decision it feeds is a
  separate spec → plan cycle.

`Closes #242` is correct and intended.

## Findings that shape the design

These were established before writing this spec and change what needs building.

### `:pinned-commit` exposure is unmeasurable on this repository

This repository has never had a submodule: no `.gitmodules` in any commit
(`git log --all -- .gitmodules` is empty), and no gitlink (mode `160000`)
entries in `HEAD`. `:pinned-commit` facts are written only by gitlink handling,
so this history produces none.

Consequence: the probe cannot produce a `:pinned-commit` exposure number, and
inventing one from a constructed fixture would measure reachability, not
frequency. The probe therefore asserts the structural fact and records it.
`:pinned-commit`'s field exposure remains **unknown** and depends on
submodule-using repositories.

### `:depends-on` exposure is plausibly real

Five positions in this history carry an author date below the running maximum —
the inverted cluster. Of those five commits, two churn Python imports:

| pos | commit | change |
|---|---|---|
| 124 | `df6b8be` | deletes `vulcan.py` and four test modules — heavy dep-edge removal |
| 125 | `0496d24` | "style: remove unused imports from conftest.py" — nothing but dep edges |
| 126 | `2d756e2` | `pyproject.toml`, `requirements.txt` — no `.py` |
| 127 | `5fbc354` | `ROADMAP.md` only |
| 128 | `f824fd9` | `ROADMAP.md` only |

So the cheap "upper bound is zero" outcome is unavailable, and the exact tier is
required.

These positions come from `git log --topo-order` over 610 commits, and differ
from #238's recorded 6 positions at 118–123 (552 commits). The probe must use
`frontier_registry.build_linearization()`, not this approximation. The
discrepancy is expected — different linearization, larger history — and is not
evidence that either measurement is wrong.

### #245's option 2 is already half-implemented for `:depends-on`

`_preload_known_deps` builds `ident_to_file` from `file_entities` and drops any
row whose source module ident is absent from it (`mcp_server.py:7526-7535`,
`7558-7560`):

```python
ident_to_file = {_code_ident("module", p): p for p in file_entities}
...
file_path = ident_to_file.get(src_ident)
if file_path is None:
    continue
```

`file_entities` comes from `_preload_known_entities`, which #238 **did**
position-filter. So dep edges whose source module failed the position filter are
already excluded — which is exactly #245's option 2, "narrow via the entity
preload," applied to the source-module side.

The real residual is therefore only the class #245 names as option 2's leak:
edges whose source module survived the position filter but whose *edge* was
introduced or closed inside the inverted window. #245's impact section is
correspondingly narrower than it reads.

This is also why the probe must drive the real `_preload_known_deps` rather than
reimplement its semantics: an offline reimplementation would miss this narrowing
and overstate the exposure.

`_preload_pinned_commits` has no equivalent narrowing — it consumes every
`:pinned-commit` fact the bound admits.

## Approach

Considered three oracles:

1. **Reimplement the bound's semantics offline** — map each fact's
   `:db/valid-from` and `:db/valid-to` to positions and compute
   misclassification arithmetically.
2. **Pure-git upper bound** — per commit, does it touch a `.py` file. Seconds to
   run, but over-approximates badly and yields no fact counts.
3. **Differential against the real function** — drive the actual
   `_preload_known_deps` at each affected position and diff against a
   position-exact offline oracle.

**Chosen: 3, built on 1's machinery.** The reason is specific to this project.
On the #238 branch a reviewer and an implementer both simulated the
counterfactual with a *date* bound instead of the real position-filtered one,
which made an inadequate test look adequate and produced a false "bug not
reachable" conclusion — two fix rounds lost. Approach 3 calls the function under
test rather than a restatement of what we believe it does. The `ident_to_file`
narrowing above is a concrete instance of what a restatement would miss.

Approach 1 remains the fallback if driving the real function proves awkward.
Approach 2 was already run as a smoke check and is what produced the table above.

## The probe

`evals/at_scale/probe_dep_preload_exposure.py`.

### Data flow

1. Ingest this repository fully into a scratch graph, via the non-polling
   in-process path — **not** `run_ingestion_benchmark`, whose poller is the
   subject of the other half of this branch.
2. `linearization = frontier_registry.build_linearization(repo_path, branch)`;
   `commit_metadata = _git_commits(repo_path, None, branch)`. Both real, and
   validated for positional alignment before use (see Error handling).
3. Build `ts → [positions]` from `commit_metadata`.
4. Derive the **affected `W` set**. A position `W` is exposed iff either
   direction is structurally possible there:

   - **wrong exclusion** — `T_hi(W) > ts(W)`, i.e. some commit at position
     `≤ W` carries a later date than `W`'s own. A fact introduced there is live
     at `W` but falls outside the `ts(W)` bound.
   - **wrong inclusion** — `min(ts[W+1 .. N-1]) <= ts(W)`, i.e. some commit
     *above* `W` carries a date at or below `W`'s own. A fact introduced there
     is not yet live at `W` but falls inside the `ts(W)` bound.

   `W` is swept if either holds. This is a position-level precondition computed
   from `commit_metadata` alone, independent of any fact; it is what keeps the
   sweep off all 610 positions.
5. For each affected `W`:
   - `_preload_known_entities(valid_at=T_hi(W), hash_to_pos=…, watermark_pos=W)`
   - feed its `file_entities` into
     `_preload_known_deps(valid_at_ms=ts(W))`
   - diff the result against the position-exact oracle for `W`.
6. Emit per-`W` wrong-inclusion and wrong-exclusion counts, the affected edge
   idents, and the diagnostics below.

### The oracle

For each `:depends-on` fact retrieved under `:any-valid-time`, invert **both**
`:db/valid-from` and `:db/valid-to` to positions through the `ts → [positions]`
map, then select the facts live at position `W` directly.

Both ends matter. `_valid_time_window_clauses` expresses the bound as half-open
containment `[?vf, ?vt) ∋ valid_at_ms` (`mcp_server.py:7466-7468`), so the
*close* timestamp is subject to date inversion on exactly the same terms as the
introduction. An oracle that inverted only `valid_from` would miss every edge
misclassified by its close — which is the direction commit `0496d24` at position
125 ("remove unused imports from conftest.py") actually exercises. The forever
sentinel maps to "not closed" and needs no inversion.

Two properties this spec states outright, because both are easy to misread
later:

- **The oracle is not a candidate fix.** It works only because the entire
  history is in hand at analysis time. A resuming forward walk has no such
  thing. It is a measurement device; it must not be read as a shipped bound, and
  it is not one of #245's three options.
- **Timestamp collisions are reported, not hidden.** Commits sharing an
  author-date second collapse to one `valid_from`, making the introducing
  position of a fact ambiguous. The probe reports the collision count next to
  the exposure number so a reader can distinguish a clean measurement from a
  smeared one.

### `:pinned-commit`

Structural check only: assert zero gitlink events across the history, and record
that exposure is therefore unmeasurable here and unknown in the field.

### Output

A JSON report plus a short human-readable summary: total affected positions,
per-`W` wrong-inclusion and wrong-exclusion counts, affected edge idents,
timestamp-collision count, unmappable-fact count, and the `:pinned-commit`
structural result.

## The #242 poller fix

`_poll_during_ingestion` gains a dedicated single-worker `ThreadPoolExecutor`
and an adaptive interval:

```python
await loop.run_in_executor(poll_executor, mcp_server.handle_minigraf_ingest_status)
await loop.run_in_executor(poll_executor, mcp_server.handle_minigraf_query, _STATUS_QUERY)
await asyncio.sleep(max(poll_interval, duty_factor * (status_d + query_d)))
```

Both halves are load-bearing. Moving the query off the event loop alone is
insufficient: `handle_minigraf_query` acquires `_db_native_lock`
(`mcp_server.py:3280`), so a growing count-scan would still serialize against
every ingestion write even from another thread. The adaptive interval is what
bounds the instrument's share of that lock.

- `duty_factor` defaults to 10, exposed as `--poll-duty-factor`. At 10 the
  poller holds `_db_native_lock` for at most ~9% of the run however large the
  scan grows.
- `_STATUS_QUERY` is **unchanged**, so the recorded latency series stays
  comparable to existing `benchmark.md` entries.
- A dedicated executor, rather than the loop default, keeps the poll off any
  thread the ingestion may want.

### Result-dict additions

`poll_count` and `poll_duty_fraction` (total poll time ÷ wall clock).
`poll_duty_fraction` is the acceptance criterion for this fix: it is direct
evidence the instrument is no longer creating the load it measures, and it
belongs in the `benchmark.md` entry beside the latency percentiles. Poll offsets
are recorded so an irregular sample stays interpretable.

### Limitations, recorded rather than papered over

- Latency percentiles are now computed over an **irregular** sample, because the
  interval adapts. The offsets are recorded for this reason.
- A cancelled run still waits on an in-flight poll thread at executor shutdown.
  The old code blocked the event loop outright, so this is strictly better, but
  it is not zero.

### `benchmark.md`

A dated note that every entry recorded before this fix carries poller overhead:
entry-to-entry comparisons remain valid, absolute figures overstate real
ingestion cost.

## Testing

### #242 — ablation-proven

A test in `tests/test_at_scale_ingestion_benchmark.py` drives
`_poll_during_ingestion` against a stub task, with `handle_minigraf_query`
monkeypatched to block synchronously, while a heartbeat coroutine ticks
concurrently. Assertions: the heartbeat keeps its cadence, and the sleep
interval backs off under a slow query.

**The ablation is mandatory.** This test is run against the *current*
implementation first and must fail there. If it passes on today's code it is not
testing anything and will be reworked or dropped — and that outcome gets
reported, not quietly absorbed. This project has already shipped four tests that
claimed guarantees they did not provide.

### Probe — test the oracle, not the number

The probe's headline output cannot be asserted; the number is what we are trying
to learn. The two components that could silently produce a *wrong* number can
be, and are:

- the `valid_from` → position inversion, and
- the affected-`W` derivation,

both against a small synthetic fixture with known inverted dates and a
deliberate timestamp collision. This is the exact error class that cost two fix
rounds on #238.

### Not tested

The ingestion the probe runs on — it is the real pipeline and already covered.

## Error handling

The probe fails loud, in deliberate contrast to the preload functions it drives
(whose per-`entity_type` `try/except: pass` is correct for their purpose and
wrong for a measurement):

- A misaligned `linearization`/`commit_metadata` pair raises, reusing
  `_load_ingestion_preload_state`'s own length-and-per-position-hash check. A
  misaligned pair mis-filters the entire sweep.
- A `valid_from` mapping to no known commit is **counted and reported**, not
  skipped. An unmappable fact means the inversion assumption is broken, which
  invalidates the measurement rather than shrinking it.

## What this branch does not decide

Whether to build #245's option 1 (record the introducing commit on both
attributes — a fact-model change requiring `MINIGRAF_SCHEMA` audit, idempotency,
and migration coverage), option 2's remaining half, option 3 (derive from git),
or to accept the residual as measured-negligible.

That decision follows the number, in its own spec → plan cycle.
