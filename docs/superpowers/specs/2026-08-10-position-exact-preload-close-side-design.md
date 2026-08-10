# Position-exact preload: the close side, `:depends-on`, and `:pinned-commit`

Design spec for the remainder of **#238** (forward-walk preload bound is
valid-time over author dates) and all of **#245** (`:depends-on` and
`:pinned-commit` preload bounds have no position handle). Part of **#222**
phase 5 hardening.

The two land together because they are one defect measured twice. PR #246 made
`_preload_known_entities` position-correct on its **introduction** side only;
its close side is still a date bound, and that date bound removes four modules
from `file_entities` before `_preload_known_deps` ever sees their edges. Fixing
the close side alone raises #245's observable loss from 2 edges to 32 without
anyone touching `:depends-on`. Deciding them together is cheaper than deciding
either alone — the standing recommendation posted on both issues.

## Problem

Ingest valid-time is denominated in **author** dates (`_git_commits` reads
`%at`, not `%ct`). Author dates are not monotonic in topological order, so a
date bound does not cleanly separate "at or below the resume position W" from
"above it". Three preload sites still decide membership by date:

| Site | Bound today | Handle available |
| --- | --- | --- |
| `_preload_known_entities`, **close** end | `valid_at = T_hi(W)` | none used |
| `_preload_known_deps` (`:depends-on`) | `[?vf, ?vt) ∋ ts(W)` | none used |
| `_preload_pinned_commits` (`:pinned-commit`) | `[?vf, ?vt) ∋ ts(W)` | none used |

Both failure directions follow, and they are not equally severe. This table is
carried forward from the #238/#231 spec unchanged, because the severities are
what every scope call below turns on:

| Preload error | Consequence | Recoverable |
| --- | --- | --- |
| **wrongly included** — live in the preload but not live at W | absent from the parse of the earlier commit being replayed, so closed and `_forget_closed_entity`-purged with an `orig_ts` later than the close's `valid_to`: an inverted valid interval | **No** |
| **wrongly excluded** — live at W but missing from the preload | replay takes the introduction branch and mints a second live `:introduced-by`, or treats an already-standing dependency as newly introduced and overwrites its true `:valid-from` | **Yes** — #235's correction sweep repairs the entity case |

### Measured

`evals/at_scale/probe_dep_preload_exposure.py`, full forward ingestion of
`master` (610 commits, head `a1c4a5f`), the real preloads driven at each of the
11 structurally affected watermark positions and diffed against a
position-exact oracle. Raw result:
`evals/at_scale/results/245-dep-preload-exposure.json`.

| | narrow (as shipped) | wide (position-correct entity set) |
| --- | ---: | ---: |
| wrongly **excluded**, distinct edges | 2 | **32** |
| wrongly excluded, position-weighted | 12 | 192 |
| wrongly **included** | 0 | 0 |
| live edges at an affected position | 38 | 68 |

At each of positions 118–123 the bound wrongly excludes **32 of 68 genuinely
live edges — 47%**. The 16× narrow/wide gap **is** #238's close-side residual:
four modules deleted by `df6b8be` at position 124 (`vulcan.py` plus three test
modules) have a close *date* below `ts(W)` but a close *position* above `W`,
so they vanish from `file_entities` at 118–123 and take 30 misclassified edges
out of both sides of the diff before the diff is computed.

Do **not** restate this as "2 of 358" or "6 of 610". Those are all-time row
counts and all-history positions; they read as ~1% and argue for closing the
issues. The decision-relevant ratios are 32 of 68 live edges at an affected
position, and 6 of 11 exposed positions.

`:pinned-commit` is **unmeasurable** on this repository — 0 gitlink events in
610 commits, so this history produces no `:pinned-commit` facts at all. Its
field exposure is unknown, not zero, and `_preload_pinned_commits` has no
`ident_to_file` narrowing at all, so it lacks even the partial mitigation that
makes `:depends-on`'s narrow figure look small.

## The premise both issues rest on, and why it no longer holds

#245 states that `:depends-on` and `:pinned-commit` "carry no commit reference
of any kind… so there is nothing to join to a `:hash` and no position to filter
on." The #238/#231 spec states that an entity's close position "is not
recoverable at all". Both are true about *joins*, and both are false about
*positions*.

Every one of these facts is dated from `commit_ts_iso`, which is
`commit_metadata[pos][1]`. A fact's `:db/valid-from` and `:db/valid-to` are
therefore always some commit's author date, and the position is recoverable by
**inverting the timestamp** rather than by joining to a `:hash`.

That was not an available option when either issue was written, and for a
concrete reason the #238/#231 spec records: the linearization did not exist yet
when the preload ran. **PR #246 removed that constraint.**
`frontier_registry.build_linearization()` and `_git_commits(repo_path, None,
branch)` now both run *above* the preload block (`mcp_server.py:10041,10047`,
consumed at `10062`), and `_load_ingestion_preload_state` validates them for
positional alignment by length **and** per-position hash equality before using
either. Full-history `commit_metadata` is in hand at preload time.

`probe_dep_preload_exposure.py` says the opposite in two places — its module
docstring ("the oracle below is NOT a candidate fix") and
`position_exact_live_edges`' docstring ("This works only because the entire
history is in hand at analysis time… A resuming forward walk has no such
thing"). That was correct against the pre-#246 shape the probe was conceived
against. It is not correct against current master, and both notes are corrected
by this change. The probe's `invert_ms_to_positions` and `edge_live_at` are
about fifteen lines and depend on nothing the preload lacks.

This is the second time on #222 that a comment about runtime behaviour outlived
the code it described and hid a fix for months — see the false
`_open_db_at` note that concealed #251/#253. The correction is part of the
work, not a footnote to it.

## Approach

### The rule

Membership in the forward walk's preload state is decided by **position
alone**:

```
in the preload at watermark position W
  ⟺  intro_pos ≤ W  AND  (close_pos is None  OR  close_pos > W)
```

Dates survive only as a query-level prefilter for row-count reduction. They
carry no safety property. This is the same demotion #238 applied to the
introduction side, extended to the close side and to the two attribute
preloads.

### The prefilter

`T_hi(W) = max(ts[0..W])` is the monotone envelope `_resume_envelope` already
computes. Two prefilter shapes are used, and they are sound for different
reasons:

**`[(<= ?vf T_hi_ms)]`, at the single-query sites** (`_preload_known_deps`,
`_preload_pinned_commits`). Sound: a fact introduced at a position `p ≤ W` has
`vf = ts[p] ≤ T_hi(W)`, so nothing that should be admitted is filtered out.
Worth having: `vf > T_hi(W)` implies `intro_pos > W`, since every position at
or below W has a date at or below the envelope, so it drops rows the position
rule would drop anyway.

**`[(<= ?vt T_hi_ms)]`, in `_preload_known_entities`' phase 2 only.** This is
*not* sound in isolation — a close above W can carry an arbitrarily early date,
which is precisely the defect. It is sound only as the **complement of phase
1**: an entity missing from a `:valid-at T_hi(W)` query has either
`vt ≤ T_hi(W)` or `vf > T_hi(W)`, and the second case implies `intro_pos > W`
and is correctly excluded. Phase 1 ∪ phase 2 therefore covers everything live
at W. Never lift this clause into a standalone query.

At no site is `?vt` given an upper bound that has to stand on its own, and at
no site is a date clause allowed to decide a row the position rule would decide
differently.

### Why this is not the "add-back union" #238 forbids

#238 warns that widening the date bound so entities closed above W stop
dropping out, *done alone*, re-admits the benign direction while leaving data
loss wide open. That warning is about a **disjunction**: admit a row if the
date bound likes it **or** some other branch does.

Here every admitted row passes the position rule. The date clause is a
prefilter applied *before* it, never an alternative to it. The re-admission
pass described below admits nothing the position rule rejects. Weakening the
prefilter can only cost query time; it cannot admit a wrong row.

## Components

### New: `_position_of_valid_time`

```python
_position_of_valid_time(ms, ts_positions, *, end) -> Optional[int]
```

- `ts_positions` maps `ts_iso -> [positions]`, built once in
  `_load_ingestion_preload_state` from the same `commit_metadata` it already
  validates hash-by-hash.
- `ms` is a `:db/valid-from` or `:db/valid-to` on minigraf's epoch-ms scale.
  It is converted with the existing
  `datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`
  pattern — second granularity, matching `_git_commits`' own format.
- `end` is `"intro"` or `"close"` and selects the collision policy below.
- Returns `None` for an instant matching no commit.

The `_VALID_TIME_FOREVER_MS` sentinel is handled by callers, not here: a `?vt`
at the sentinel means "still open" and never reaches this function.

### Ambiguity policy — asymmetric, and the inverse of the probe's

`_git_commits` formats `%Y-%m-%dT%H:%M:%SZ`, so distinct commits routinely
share an instant and `ts_positions[ts]` can hold several positions. Resolution
is always toward **wrong-exclusion**, the recoverable direction:

| end | ambiguous → take | biases toward | direction |
| --- | --- | --- | --- |
| introduction | `max` (latest colliding position) | `intro_pos > W` → excluded | recoverable |
| close | `min` (earliest colliding position) | `close_pos ≤ W` → excluded | recoverable |

`probe_dep_preload_exposure.py`'s `edge_live_at` resolves the **opposite** way —
`min` for an ambiguous introduction, `max` for an ambiguous close — and its
docstring explains why: a measurement must not understate exposure, because
understating would argue for closing #245 on a number rounded in our own
favour. A fix must not risk the unrecoverable direction.

**Both are correct for their purpose, and they must never be refactored into a
shared helper.** This is the single most likely future mistake in this change.
It is stated in both docstrings and pinned by its own test.

### Unmappable instants

An instant matching no commit means either the history was rewritten under the
graph, or a fact was dated by something other than a commit. By construction
neither should happen: every `:depends-on`, `:pinned-commit` and `:ident`
valid-time is written from `commit_ts_iso`.

Policy: **exclude, and count.** Excluding is the recoverable direction. A
run-scoped counter is printed to stderr at the end of the preload when nonzero,
mirroring the "announce the degradation instead of failing silently" pattern
`_commit_date_query` already uses when a non-empty watermark has no `:date`.

Not a hard failure. Aborting ingestion because one fact is unplaceable is worse
than excluding it, and the ordinary rewritten-history case is already covered
by the degradation path below.

### `_preload_known_entities` — the close side

Two phases, deliberately, because this query loads **values** and not just
membership. Rewriting it wholesale into `:any-valid-time` would return every
historical `:description` of every entity and require interval selection per
attribute; `:description` is rewritten on every body edit, so that is a row
explosion in exchange for a hazard nobody has measured (see "Out of scope").

**Phase 1** — today's query, unchanged, at `:valid-at T_hi(W)`, with the
existing conjunctive introduction position clause. Yields everything live at
that instant.

**Phase 2** — one query per `entity_type` binding *only* the `:ident` fact's
window:

```
[:find ?ident ?vf ?vt :any-valid-time
 :where [?e :entity-type :type/X]
        [?e :ident ?ident]
        [?e :db/valid-from ?vf]
        [?e :db/valid-to ?vt]
        [(<= ?vt T_hi_ms)]]
```

`:ident` is written once per entity lifetime, so this is roughly one interval
per entity rather than one per commit. The `[(<= ?vt T_hi_ms)]` prefilter is
exactly the set phase 1 missed: an entity absent from phase 1 either has
`vt ≤ T_hi(W)` (closed early *by date*) or `vf > T_hi(W)`, and the latter
implies `intro_pos > W` and is correctly excluded. The sentinel is excluded by
the same clause, so no separate `< FOREVER` test is needed.

Clause order matters and is load-bearing: minigraf's `:db/valid-from` and
`:db/valid-to` pseudo-attributes bind to whichever EAV clause on `?e` most
recently precedes them, so `[?e :ident ?ident]` must sit immediately above
them. This is the same constraint `_preload_known_deps` and
`_preload_pinned_commits` already document.

Rows are kept where `interval_intro_pos ≤ W < close_pos`, where
`interval_intro_pos` inverts the `:ident` fact's own `?vf` under the `"intro"`
policy and `close_pos` inverts `?vt` under the `"close"` policy. An entity with
several `:ident` intervals — deleted and re-added — contributes at most one:
the interval satisfying that test. If none does, the entity is not live at W.

Note this is the **interval's** start, not the authoritative `:introduced-by`
position phase 1 gates on. That is deliberate and sufficient: it is doing
interval *selection* here, and the authoritative introduction gate is applied
independently by the re-admission query below, which is phase 1's query and
carries phase 1's position clause. Both gates apply to every re-admitted row,
conjunctively.

**Re-admission** — survivors not already present in phase 1 get their values by
**re-running phase 1's query verbatim** at `:valid-at ISO(vt − 1 ms)`, the last
instant the entity was live, once per *distinct* closing instant among the
survivors. On this repository that is one extra query, for `df6b8be`.

Re-running the existing query rather than issuing a per-ident lookup avoids a
new query shape and avoids escaping entity idents into subject position. Rows
from the re-admission pass are filtered to the survivor set and merged only for
idents phase 1 did not already supply, so a re-introduced entity keeps its
current values rather than being overwritten by a historical version.

`file_entities`, `submodule_paths`, `entity_valid_from`, `entity_descriptions`
and `entity_introduced_by` are all populated from re-admitted rows exactly as
from phase 1 rows.

### `_preload_known_deps` and `_preload_pinned_commits`

Both already bind `?vf` and `?vt` under `:any-valid-time`, so the change is
small:

- add `?vt` to `:find`;
- replace `_valid_time_window_clauses(valid_at_ms)` with `[(<= ?vf T_hi_ms)]`;
- filter in Python: `intro_pos ≤ W`, and `vt == _VALID_TIME_FOREVER_MS` or
  `close_pos > W`.

Signatures take `ts_positions` and `watermark_pos` in place of `valid_at_ms`,
plus `t_hi_ms` for the prefilter. `_load_ingestion_preload_state` stops passing
them `resume_valid_at_ms`.

`_preload_known_deps` keeps its `ident_to_file` narrowing against
`file_entities` unchanged. That narrowing is what makes the entity close-side
fix visible here: once the four modules are re-admitted, their 30 edges enter
the query's consideration and the edge-level position filter then decides them
correctly.

`:pinned-commit` gets the identical treatment on the argument that it shares
the mechanism, **not on evidence**. Its exposure is unmeasurable on this
history and stays so; the spec records that rather than implying it was
verified.

### `_load_ingestion_preload_state`

Builds `ts_positions` from `commit_metadata` alongside the existing
`hash_to_pos`, computes `t_hi_ms = _iso_to_epoch_ms(_resume_envelope(...))`,
and threads `ts_positions` / `watermark_pos` / `t_hi_ms` to the three sites.

`resume_valid_at` survives for `_preload_unresolved_dep_idents` only.

### Not changed

`_preload_unresolved_dep_idents` keeps `ts(W)`. Its subtrahend was already
decoupled from the resume position by #246, and its own docstring argues a
narrower bound is safer for a stub set. Making it position-exact is unrelated
scope.

## Assumptions to verify against the backend before building on them

Two backend behaviours this design assumes are not exercised by any current
code path. Both are cheap to check empirically against a real graph, and both
must be checked **first**, because a fallback shape differs materially:

1. **`[(<= ?vt N)]` binds against the `:ident` fact under `:any-valid-time`.**
   Every existing predicate on a pseudo-attribute is generated by
   `_valid_time_window_clauses`, which only ever emits `[(= ?vt FOREVER)]` or
   the `[(<= ?vf N)] [(> ?vt N)]` pair. An upper bound on `?vt` alone is new.
   If it does not bind, phase 2 drops the clause and filters `?vt` in Python at
   the cost of scanning every `:ident` interval.
2. **`:valid-at` accepts a millisecond-precision ISO timestamp.** The
   re-admission pass queries at `ISO(vt − 1 ms)`, which carries a `.%f` field;
   every existing `:valid-at` caller passes a second-granularity commit date.
   If millisecond precision is rejected or silently truncated, the re-admission
   instant becomes `ISO(vt) − 1 s`, which is equivalent here only because
   author dates are second-granular — so the fallback is safe but must be
   chosen deliberately rather than discovered.

Standing lesson on this project: minigraf's Rust source is checked out locally
at `~/Work/AMC/Minigraf/minigraf`. Read it, or test against a real graph. Do
not infer either behaviour from our own comments — a false comment about
minigraf's runtime behaviour hid #251/#253 for months.

## Error handling

The per-`entity_type` `try/except: pass` in `_preload_known_entities` stays as
is, and phase 2 is wrapped the same way: a phase-2 failure degrades to today's
behaviour rather than emptying the preload.

`watermark_pos is None` — a fresh graph, or a watermark absent from this
linearization because the history was rewritten — disables every position
clause and every prefilter, restoring the pre-#222 unrestricted queries
exactly. This is unchanged from #246 and is what makes the unmappable case rare
in practice: a rewritten history usually fails at the watermark lookup first.

The existing positional-alignment validation in `_load_ingestion_preload_state`
(length **and** per-position hash equality) already guards `ts_positions`,
which is derived from the same list. No new validation is required.

## Migration

None. No new attribute is introduced, so there is no `MINIGRAF_SCHEMA`
registration and no registered-type audit obligation. Existing graphs are read
through a different filter, never rewritten.

Graphs carrying stale live `:introduced-by` on already-closed entities — the
#231 residue — are unaffected: phase 2 tests the `:ident` fact's window, not
`:introduced-by`. That litter remains #244's territory.

## Testing

Real-backend only, per `docs/testing-conventions.md`, following the `real_db`
seeding pattern already established for the preloads in
`tests/test_mcp_server.py`.

1. **Entity close side, both directions.** At an inverted position: an entity
   closed *above* W with an earlier close date is present (fails on master); an
   entity closed *at or below* W with a later close date is absent.
2. **`:depends-on`, both directions.** Same shape. The narrow case must be
   driven by the edge's **close**, not its introduction — an oracle inverting
   only `:db/valid-from` reported zero exposure during #245's measurement and
   would have argued for closing the issue. This test exists to keep that
   near-miss from recurring.
3. **`:pinned-commit`, both directions.** Synthetic fixture, since no real
   history here produces these facts.
4. **Collision policy.** Two commits sharing an instant, one on each side of W:
   the exclusion-biased choice is taken at both ends. This is the test that
   stops a later refactor unifying the fix's policy with the probe's.
5. **Unmappable instant.** A fact dated off-linearization is excluded and
   counted — not admitted by a date fallback.
6. **Re-admission is position-gated.** Same fixture as test 1 with the position
   arguments omitted: the above-W entity returns. The close-side twin of the
   existing `test_the_envelope_alone_does_not_close_the_hole`, pinning that the
   widened prefilter alone is not the safety mechanism.
7. **End-to-end resume.** A fixture repo with deliberately non-monotonic author
   dates, ingested to a watermark at an inverted position and then resumed,
   following `test_resumes_from_watermark_after_shutdown`'s shape. #238 requires
   this specifically: "any regression test for this needs to construct the
   resume explicitly, not rely on a fresh ingestion." Every per-task review
   during #222 phase 2d saw only fresh runs and structurally could not observe
   the bug.

## Acceptance

Re-run `probe_dep_preload_exposure.py` against the fixed preloads and require:

- `wrongly_excluded == 0` and `wrongly_included == 0`, in **both** the narrow
  and the wide framing;
- `timestamp_collisions == 0` (without which the fix's and the oracle's
  opposite ambiguity policies can legitimately disagree, invalidating the
  comparison);
- all four unmappable counters at 0.

This requires a probe change: a mode that drives the *fixed* preloads, and an
acknowledgement that the narrow/wide distinction collapses once the close side
is position-correct — the two framings converge by construction, which is
itself the finding to report.

**Recorded honestly as partly self-referential.** The oracle and the fix share
an algorithm, so agreement checks the *plumbing* — the fix runs inside
`_load_ingestion_preload_state` with only W in hand, through the real queries,
the real `entity_type` loop and the real `ident_to_file` narrowing, while the
oracle runs offline with the whole history — and not the algorithm. Tests 1–7
are the non-circular evidence and are what the close decision rests on.

## Out of scope

- **Position-exact attribute *values*.** `entity_descriptions` still takes
  whichever `:description` version was live at date `T_hi(W)`, which can be a
  version written above W with an inverted author date. The forward walk uses
  that dict for body-change detection, so a from-the-future description can
  make a real change at W+1 compare equal and never be recorded. Unmeasured,
  and a much larger change (per-attribute interval inversion over an attribute
  rewritten on every body edit). **A new issue is filed as part of this work**,
  carrying the mechanism sketch and the fact that the exposure is unmeasured,
  and `_preload_known_entities`' docstring points at it.
- `_preload_unresolved_dep_idents`' `ts(W)` bound, per "Not changed" above.
- Recording the closing commit in the graph (`:closed-in`), or reifying
  `:depends-on` edges to carry a commit reference. Both are exact and neither is
  needed once the timestamp is invertible; both carry schema-audit,
  idempotency and migration obligations, and reification would multiply
  `:depends-on` write volume under minigraf#287.
- Deriving the resolved-import set and gitlink tree from git at W (#245's
  option 3). Exact for these two sites, useless for the entity close side, and
  re-does the resolution work the preload exists to avoid.
- Cleaning up stale live `:introduced-by` on already-closed entities in
  existing graphs — #244.

## Issue hygiene

This work **closes #238 and closes #245**. **#222 stays open** — `Refs #222`,
never a closing keyword.

GitHub scans closing keywords in commit messages **and** the PR body, and on
this project a *negated* "does not close #N" has still auto-closed an issue.
Keyword placement is therefore re-scanned after **every** commit written on the
branch, not once before the push.

## Documentation

`SKILL.md` and `CLAUDE.md` need no change: no query syntax, attribute, or tool
surface moves. The contract that changes is internal to ingestion and is
documented in the affected docstrings.

Two stale records are corrected as part of this change:

- `probe_dep_preload_exposure.py`'s module docstring and
  `position_exact_live_edges`' docstring, both of which assert the inversion
  cannot be a fix. True before #246, false after it.
- The `_preload_known_entities` docstring's claim that the close end "is not
  recoverable at all", and the corresponding claim in
  `2026-08-04-position-indexed-preload-design.md`. That spec gets a revision
  section rather than an edit in place, matching how
  `2026-07-31-reverse-walk-write-amplification-design.md` records its own
  corrections.
