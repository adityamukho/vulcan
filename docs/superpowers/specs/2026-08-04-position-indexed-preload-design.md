# Position-indexed forward-walk preload, and closing `:introduced-by`

Design spec for **#238** (forward-walk preload bound is valid-time over author
dates) and **#231** (`_build_close_triples` never retracts `:introduced-by`).
Part of **#222** phase 5 hardening.

The two issues land together because #231 alone converts #238's transient data
loss into permanent loss plus a duplicate `:introduced-by`. They also converge
on one implementation detail: both need the introducing commit's identity,
which the preload query can bind with a single extra variable.

## Problem

`_reload_walk_state` bounds the forward walk's mutable preload state with
`resume_valid_at = _commit_date_query(db, watermark)` — the watermark commit's
own `:date`. That bound is expressed in valid-time, and ingest valid-time is
denominated in **author** dates (`_git_commits` reads `%at`, not `%ct`). Author
dates are not monotonic in topological order, so the bound does not cleanly
separate "at or below the resume position" from "above it".

Two failure directions follow, and they are not equally severe:

| Preload error | Consequence | Recoverable |
| --- | --- | --- |
| Entity **wrongly included** — introduced above W, dated earlier | absent from the parse of the earlier commit being replayed, so closed and `_forget_closed_entity`-purged, with an `orig_ts` later than the close's `valid_to`: an inverted valid interval | **No** — permanent data loss |
| Entity **wrongly excluded** — live at W but missing | replay takes `_build_code_triples`' introduction branch and mints a second live `:introduced-by` | **Yes** — #235's correction sweep repairs it |

Measured on this repository: of 552 watermark positions, 6 (positions 118-123)
have a strictly-earlier-dated later position. Positions 124-128 are a side
branch authored 2026-04-26/27 that lands topologically after the 2026-05-02
merges, confirmed descendants of 118-123 via `git merge-base --is-ancestor`.
Resuming at any of those 6 positions keeps the data-loss direction open.

Separately, `_build_close_triples` closes `:ident`, `:description`,
`:contains`, `:class`, `:entity-type`, `:path`/`:file` and `:static` — but
never `:introduced-by`. A closed-and-purged entity therefore still answers a
bare `[?e :introduced-by ?c]` query, which makes
`_entity_introduced_by_query(db, ident) is not None` an unsound "do I already
know this entity?" test — and that is exactly how `_reverse_apply` gates known
entities. The reverse walk answers "known" for an ident the forward walk closed
and purged, resurrects it, and never re-asserts `:ident`: an entity with
lineage but no identity.

## Why the bound is in valid-time today

Not preference — availability. `_load_ingestion_preload_state` runs at
`mcp_server.py:9441`; `frontier_registry.build_linearization()` is not called
until `mcp_server.py:9453`. The linearization simply does not exist yet when
the preload runs. `build_linearization` needs only `repo_path` and `branch`, no
DB handle, so this is an ordering artifact and not a constraint.

## The asymmetry that decides the design

With #231 fixed, an entity's **introduction position** is exactly recoverable:
`[?e :introduced-by ?c] [?c :hash ?h]` → `hash_to_pos[?h]`.

Its **close position** is not recoverable at all. `_ingest_close`
(`mcp_server.py:4786`) records a close as `valid_to = commit_ts_iso` and holds
no reference to the closing commit. Recovering it would mean recording the
closing commit in the graph — a fact-model change with schema/audit,
idempotency and migration obligations, and a new triple per close.

That asymmetry is decisive, because wrong-*inclusion* — the unrecoverable
direction — is caused **solely** by the introduction end. So the unrecoverable
direction can be closed exactly, with no fact-model change, by filtering on the
one thing that is exactly available.

## Approach

Two clauses, applied to every row of the preload query:

```
row survives  ⟺  hash_to_pos[row.introduced_by_hash] <= watermark_pos
                 AND row visible at :valid-at T_hi(W)
```

where `T_hi(W) = max(ts[0..W])` over `commit_metadata` — the monotone envelope
of every author date at or below the resume position.

**The position clause is conjunctive over every row, not a second branch.**
That is what makes widening the date bound safe, and it is the distinction
#238 insists on. The instinct is to widen the valid-time bound so entities
closed above W stop dropping out; done alone that is the "add-back union" the
issue warns produces a change that looks like a fix and isn't, because it
re-admits the benign direction while leaving data loss wide open. Here the
position clause gates every row regardless of what the date clause admits, so
the date bound is demoted from safety mechanism to "how widely we re-admit
entities closed above W". `T_hi(W)` is the widest value that still excludes
every close at or below W: a close at position p ≤ W has
`valid_to = ts[p] <= T_hi(W)`, and `:valid-at`'s half-open semantics require
`valid_at < valid_to`, so it is excluded.

This **replaces** the old bound rather than unioning with it: `resume_valid_at`
for these sites becomes `T_hi(W)` instead of `ts(W)`, and the position clause
is new.

### Resulting behaviour

| Case | Today (`ts(W)`) | After |
| --- | --- | --- |
| intro above W, dated earlier — **data loss** | included ✗ | **excluded ✓** (position clause) |
| intro at/below W, dated later — duplicate | excluded ✗ | **included ✓** (envelope) |
| closed above W, close dated earlier | excluded | excluded — residual |

### The surviving residual

One case remains: an entity introduced at or below W, deleted or renamed above
W with a close date earlier than `T_hi(W)`, where Stage B's lifecycle pass
already applied that deletion in a prior run. It is excluded from the preload,
so replay mints a duplicate `:introduced-by`.

This is accepted deliberately. It is the recoverable direction, and it is
narrowed further by #235: `mcp_server.py:8388-8430` makes
`_lineage_is_provisional(db, ident)` the sole authority for reconcilability, so
a still-provisional entity is reconciled in place by
`_forward_reconcile_provisional` rather than duplicated. Only **authoritative**
entities reach the duplicate path, and there the correction sweep's #235 repair
collapses them.

## Components

### `_preload_known_entities`

Signature gains `hash_to_pos: Optional[Dict[str, int]]` and
`watermark_pos: Optional[int]`. `valid_at` stays, now fed `T_hi(W)`.

Query gains `?hash` in `:find` and `[?c :hash ?hash]` in `:where`, alongside
the existing `[?c :date ?date]`. Rows are filtered in Python:

```python
pos = hash_to_pos.get(hash_) if hash_to_pos is not None else None
if watermark_pos is not None and (pos is None or pos > watermark_pos):
    continue
```

`pos is None` means the introducing commit is not in this linearization — a
rewritten or foreign history — and excludes, which is the benign direction.
All three arguments `None` together restore today's unrestricted behaviour
exactly, which is what a fresh graph (no watermark) wants.

The same row now also yields `entity_introduced_by[ident] = f":commit/{hash_[:12]}"`,
a fifth return value and #231's retract value. One extra bound variable serves
both fixes; this is the concrete reason the two issues are one change.

### `_build_close_triples`

New keyword `introduced_by: Optional[str] = None`, appending
`[{ident} :introduced-by {introduced_by}]` when given.

Opt-in, like `close_entity_type` / `file_value` / `is_static`, for the same
reason those are: unresolved-import stubs reuse the module ident prefix and
never carry `:introduced-by` (`mcp_server.py:8521-8525`), so deriving one from
the ident would retract a fact that was never asserted.

### `_ForwardWalkState.entity_introduced_by`

A new `Dict[str, str]` maintained beside `entity_valid_from`:

- **set** at the five introduction branches in `_build_code_triples` (module,
  function, class, global, field), from the `commit_ident` that function
  already receives;
- **seeded** by `_preload_known_entities`' new return value;
- **popped** by `_forget_closed_entity`, alongside the dicts it already purges
  and for the same reason;
- **read** at the six `_build_close_triples` call sites (`mcp_server.py`
  8320, 8357, 8496, 8564, 8603, 8677), each of which is already paired with a
  `_forget_closed_entity` call.

**Known hole, and its fallback.** Entities the *reverse* walk introduced during
this run were never written through the forward state, so Stage B's
`_forward_apply(lifecycle_only=True)` closing them finds no entry — and #231's
bug would survive for exactly those. Close sites therefore fall back to
`_entity_introduced_by_query(db, ident)` on a miss. That is a per-close DB read,
not a per-ident-per-commit one, so it does not feed #239's hot path.

### Lineage marker discard on close

Each close site also calls `_lineage_confirm(db, ident)` (which retracts the
`:type/lineage-marker` companion entity, no-op when absent). Without it a
re-introduction at the same ident inherits a stale provisional marker and is
treated as provisional by `_lineage_is_provisional` — the sole authority since
#235.

### `_reverse_apply`'s known-entity gate

`mcp_server.py:7999`'s `_entity_introduced_by_query(db, ident) is not None`
becomes a live-`:ident` test via a new `_entity_ident_is_live(db, ident)`
helper.

Closing `:introduced-by` alone would make the existing gate correct —
`_entity_introduced_by_values_query` queries at current time — but the gate's
real question is liveness, and binding it to a lineage attribute is what made
#231 possible. The liveness test stays correct even if a future close site
forgets `:introduced-by`. It is the same one query per candidate ident, so
#239's cost profile is unchanged.

### `_preload_unresolved_dep_idents`

Subtracts a new **unbounded** `:path`-bearing external-dependency query instead
of the now-position-filtered `submodule_paths`.

Necessary, not incidental: once `submodule_paths` is position-filtered, a real
submodule born above W drops out of the subtrahend but stays in the minuend and
is misclassified as a stub — reaching `state.unresolved_dep_idents`, where the
replayed gitlink "add" handler's `_submodule_path_matches_import` check can
fire on it and mint a bogus `[:module/sub-b :resolves-to :module/sub-a]`. That
is the exact failure the function's own docstring warns about, reintroduced by
this change unless the subtrahend is decoupled.

Decoupling is also the correct semantics independently: stub-ness is "has no
`:path`", a property of the entity, not of the resume position.

### `_preload_known_deps` and `_preload_pinned_commits`

**Unchanged**, still bounded at `ts(W)`.

`:depends-on` and `:pinned-commit` facts hold no commit reference of any kind,
so no position clause is available for them. Widening them to the envelope
without a position clause would make their data-loss direction *worse*, which
is precisely the union #238 forbids. Leaving them at `ts(W)` is strictly no
worse than today.

Their docstrings gain a note recording this residual and its cause, and a
follow-up issue is filed. #238's measured data loss is entity-level; this keeps
the change focused on it.

### Call-order change in `_run_ingestion`

`frontier_registry.build_linearization()` and `_git_commits(repo_path, None,
branch)` move **above** the `_load_ingestion_preload_state` block at
`mcp_server.py:9440` — ahead of the DB open, not between it and the
`_db = None` lock release, so the graph file lock is held for no longer than it
is today.

`_load_ingestion_preload_state` gains `linearization` and `commit_metadata`
parameters, derives `hash_to_pos`, resolves `watermark_pos` from the watermark
hash, and computes `T_hi(W)`.

## Error handling

The preload's per-`entity_type` `try/except: pass` stays as-is.

`_load_ingestion_preload_state` asserts `linearization` and `commit_metadata`
are positionally aligned before using them — the same check `_reverse_apply`
already performs at `mcp_server.py:7955`. A misaligned pair here silently
mis-filters the entire preload rather than raising, which is worse than the
misattribution that check exists to prevent.

A watermark hash absent from the linearization yields `watermark_pos = None`,
which disables the position clause and degrades to today's behaviour rather
than filtering everything out.

## Migration

None required.

Existing graphs carry closed entities with a stale live `:introduced-by` — the
#231 bug's residue. A stale-live `:introduced-by` cannot by itself pull such an
entity into the preload: the query also requires that entity's `:ident`,
`:path` and `:description` be visible at the same bound, so the row appears
only when the entity's `:ident` window covers `T_hi(W)` — i.e. exactly when it
was closed above W and *should* be included. The new gate tests `:ident`, so it
ignores the stale fact too. It remains as graph litter, which is #244's
territory (recovery for already-completed graphs).

No new attribute is introduced, so there is no `MINIGRAF_SCHEMA` registration
or registered-type audit obligation.

## Testing

Real-backend only, per `docs/testing-conventions.md`. Tests 1-7 follow the
`real_db`-seeding pattern already established at
`tests/test_mcp_server.py:9345`, which hand-crafts `:valid-from` timestamps and
calls the preload directly.

1. **Data-loss direction.** Entity introduced above W at an earlier date must
   be excluded. Fails on master.
2. **Duplicate direction.** Entity introduced at or below W at a later date
   must be included. Fails on master.
3. **The envelope is not the safety net.** Same fixture, called with
   `valid_at=T_hi(W)` but the position arguments omitted: the above-W entity
   returns. Pins that the position clause closes the hole and that the widened
   date bound *alone* is unsafe — the single most likely thing for a later
   refactor to get backwards, and the exact shape #238 warns against.
4. **`_preload_unresolved_dep_idents`.** A real submodule born above W is not
   misclassified as a stub. Reworks the existing test at
   `tests/test_mcp_server.py:9345`, whose current assertion depends on the
   bounded subtrahend this change removes.
5. **`_build_close_triples`.** Emits the `:introduced-by` retract when given a
   commit ident, omits it when not.
6. **Stage B fallback.** An entity introduced by the reverse walk and closed by
   `_forward_apply(lifecycle_only=True)` gets its `:introduced-by` retracted
   via the `_entity_introduced_by_query` fallback.
7. **Lineage marker discard.** A re-introduction at a previously-closed ident
   is not provisional.
8. **The three markers come off.** The `strict=True` xfail at
   `tests/test_mcp_server.py:11907` and the two
   `MINIGRAF_INGEST_STREAM_RATIO=1000000:1` pins at
   `tests/test_mcp_server.py:11841`. `strict=True` means that xfail fails the
   run the moment the gate is fixed, so this is forced rather than optional.
9. **End-to-end resume.** A fixture repo with deliberately non-monotonic author
   dates, ingested to a watermark at an inverted position and then resumed,
   following `test_resumes_from_watermark_after_shutdown`'s shape
   (`tests/test_mcp_server.py:10701`). #238 specifically requires this: "any
   regression test for this needs to construct the resume explicitly, not rely
   on a fresh ingestion." Every per-task review during #222 phase 2d saw only
   fresh runs and structurally could not observe the bug.

## Out of scope

- Recording the closing commit in the graph (`:closed-in` / `:position`), which
  would make the close end exactly filterable. Rejected: fact-model change with
  migration obligations, to close only the recoverable direction.
- Position-filtering `:depends-on` and `:pinned-commit` — see above; follow-up
  issue.
- Cleaning up stale live `:introduced-by` on already-closed entities in
  existing graphs — #244.
- `_db_checkpoint` cadence (#241) and the `:introduced-by` point-query volume
  (#239). This change is cost-neutral by construction: the gate keeps the same
  one query per candidate ident, and the new fallback fires per close, not per
  ident per commit.

## Documentation

`SKILL.md` needs no change — no query syntax, attribute or tool surface
changes. `CLAUDE.md` likewise. The behavioural contract that changes is
internal to ingestion and documented in the affected docstrings.
