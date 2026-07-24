# Reverse-Bulk-Fill Walk (Stream 2) — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2b of 4)
**Date:** 2026-07-24

## Background

#222's overall design is a converging multi-stream ingestion: a forward-truth
stream (the existing `_run_ingestion` engine) running concurrently with a
reverse-bulk-fill stream that provisionally back-fills recent history from
`HEAD` downward, so recent history is visible almost immediately while the
forward stream still owns lineage correctness.

- **Phase 1** (merged, PR #226) built `frontier_registry.py`
  (`Interval`/`FrontierAllocator`/`build_linearization`) plus
  `mcp_server.py`'s `_frontier_load`/`_frontier_persist_claim`/
  `_frontier_read_bounds` — the shared-gap allocator both streams claim work
  from. Nothing calls `FrontierAllocator.claim_low`/`claim_high` yet.
- **Phase 2a** (merged, PR #227) built the provisional/authoritative fact
  model primitives with no caller: `_lineage_mark_provisional`/
  `_lineage_confirm`/`_lineage_is_provisional` (a companion
  `:type/lineage-marker` entity per tracked entity, since `:lineage-status`
  can't live directly on schema-audited code entities — see that spec's
  Revision note #1), `_lineage_confirmed_through_query`/`_update`/`_migrate`
  (a watermark for "is region X's lineage fully confirmed"), and
  `_candidate_diff_persist`/`_read`/`_clear` (per-`(commit, entity)`
  body-hash records so Stream 1 can later confirm/reject a candidate by hash
  comparison instead of re-parsing).
- **2b (this spec)** — Stream 2's actual reverse-bulk-fill walk, using 2a's
  primitives to write provisional lineage. No caller wired into
  `_run_ingestion` yet — that's 2d.
- **2c** — Stream 1's correction sweep, converting provisional facts to
  authoritative using 2a's persisted candidate diffs.
- **2d** — the actual concurrency wiring inside `_run_ingestion`.

## Scope (2b only)

In scope:

- A per-commit reverse-fill step function that claims one position from the
  gap (`FrontierAllocator.claim_high()`) and, for every entity touched by
  that commit's diff, writes structural facts + a `:modified-in` edge
  (always authoritative — order-independent per the issue's own design
  principle) + a provisional `:introduced-by` (using 2a's primitives).
- A thin driving loop that repeats the above until the gap closes and
  persists each claim via `_frontier_persist_claim`.
- Two small new helpers this sub-phase needs and 2a didn't provide:
  `_entity_introduced_by_query` (read the current `:introduced-by` value, if
  any) and `_entity_introduced_by_set_provisional` (idempotent
  retract-then-reassert, gated so it never clobbers an already-authoritative
  fact).

Explicitly deferred (matches the issue's own phase breakdown and this
sub-phase's tighter cut):

- `:depends-on` edges, renames (`:renamed-from`/`:renamed-to`), and
  deletion/close handling in the reverse direction. Forward walk's existing
  logic for these is intentionally not mirrored here.
- The "entity born, removed, and reborn entirely within the open gap" edge
  case from the issue — since 2b never closes a lifecycle segment, this
  case is 2c's reconciliation problem, not 2b's.
- Reconciling a *stale* candidate-diff record left behind when reverse walk
  moves a candidate's `:introduced-by` earlier (see below) — 2c's job.
- Wiring into `_run_ingestion` / real concurrency (2d).

## Design

### Schema/audit safety

Unlike 2a's `:type/lineage-marker`/`:type/candidate-diff` (deliberately
unregistered, internal bookkeeping), `:introduced-by` and `:modified-in`
are **already** registered optional attributes on every entity type this
sub-phase touches — `module`, `function`, `class`, `variable`, `field` all
list both in `MINIGRAF_SCHEMA`'s `"optional"` set (mcp_server.py:5307-5352).
Forward walk already writes both today. 2b introduces no new schema
surface and no new audit risk: `handle_minigraf_audit` already treats these
facts as valid for these types, provisional or not — provisionality is
carried entirely by the separate `:type/lineage-marker` companion entity
2a built, never by the shape of the `:introduced-by` fact itself.

### Resume-safety / atomicity boundary

`_run_ingestion` checkpoints once per source-commit — an entire commit's
`add_triples`/`close_items` batch is one `_transact` + one fsync'd
checkpoint, never split across two checkpoints (see its `write_executor`
docstring comments on "committed once per source-commit"). A crash
mid-commit therefore leaves that commit's writes entirely uncommitted, and
`_git_commits(repo_path, watermark, branch)` re-derives the resume point
from the DB's actual persisted state, not an in-memory cursor — so a
reprocessed commit never sees its own partial writes still present.

2b's `_reverse_fill_claim_and_process` must follow the same discipline: one
claimed commit's structural facts, `:modified-in` edges,
`:introduced-by` write, and candidate-diff persist all happen within that
call's own atomic write boundary, with `_frontier_persist_claim` recording
the claim as part of the same unit of work. A crash before that boundary
completes means the position was never actually claimed (from the
allocator's persisted perspective — see phase 1's `_frontier_persist_claim`
design), so re-invoking `_reverse_fill_claim_and_process` against the same
still-unclaimed position on resume is safe: it either finds nothing was
written yet (clean retry) or, at worst, hits `_entity_introduced_by_set_provisional`'s
own idempotency guard for any entity whose facts *did* land before the
crash (same value → no-op).

### Why `_extract_commit` needs no changes

`_extract_commit` (mcp_server.py:6888) is already a pure, stateless function
of one commit: it diffs that commit against its own parent(s) via
`git diff-tree`, independent of any accumulated walk state, and (for
`M`/`D`/`R` files) already fetches and parses the parent's blob content
(`old_sha` → `_git_blob_content` → `old_parser.parse`) to support rename
detection and #221's unchanged-body detection. It is safe to call from a
reverse walk exactly as forward walk calls it today — no walk-direction
dependency exists in this function.

What genuinely differs between forward and reverse is the **bookkeeping
layer**: forward walk's `_build_code_triples` (mcp_server.py:6399) decides
"is this the first time I've ever seen this ident" purely from its own
in-memory `entity_valid_from` dict, accumulated oldest→newest. Reverse walk
has no equivalent accumulated-forward state (it's walking backward through
territory forward hasn't reached), so it substitutes a **DB query** for the
same question — resume-safe, since the gap allocator is itself resumed from
persisted state, not memory (Phase 1's own design principle).

### New helpers

```python
def _entity_introduced_by_query(db: Any, entity_ident: str) -> Optional[str]:
    """Return entity_ident's current :introduced-by value (a commit ident
    string), or None if it has none yet."""
    raw = _db_execute(db, f"(query [:find ?c :where [{entity_ident} :introduced-by ?c]])")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _entity_introduced_by_set_provisional(
    db: Any,
    entity_ident: str,
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Assert or move entity_ident's PROVISIONAL :introduced-by to
    commit_ident. Never touches an entity whose :introduced-by is already
    authoritative (i.e. a fact exists and _lineage_is_provisional is False)
    -- reverse walk must never clobber a fact Stream 1 has already
    confirmed. Idempotent: no-ops (but still ensures the marker is present)
    if the current value already equals commit_ident. Always ensures
    _lineage_mark_provisional is called when a provisional value is
    asserted or moved -- that call is itself idempotent, so calling it
    unconditionally on the write path is safe.
    """
    current = _entity_introduced_by_query(db, entity_ident)
    if current is not None and not _lineage_is_provisional(db, entity_ident):
        return  # authoritative -- never touch
    if current == commit_ident:
        _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=index_con)
        return
    if current is not None:
        _retract(db, f"[[{entity_ident} :introduced-by {current}]]", index_con=index_con)
    _transact(db, f"[[{entity_ident} :introduced-by {commit_ident}]]", commit_ts_iso, index_con=index_con)
    _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=index_con)
```

### Per-commit algorithm

For a claimed commit `C` (positions processed strictly newest→oldest via
repeated `claim_high()` calls), for every entity touched by `C`'s diff
(reusing the same per-file A/M walk `_build_code_triples` already performs
— entity discovery/parsing is unchanged, only the bookkeeping gate differs):

1. **Structural facts**: gate on `_entity_introduced_by_query(db,
   entity_ident) is None` — if the entity has no `:introduced-by` fact yet
   from *either* stream, write its structural facts (`:entity-type`,
   `:description`, `:file`/`:path`, `:contains`, matching forward's
   candidate-triple shape) as well as its first provisional
   `:introduced-by` (step 2 below). If the entity already has a fact (from
   either stream), only a `:modified-in` edge is added — never
   re-asserted structural attributes, matching forward's existing
   "written once" invariant (mcp_server.py:6414).
2. **`:introduced-by`**:
   - **No fact yet** → `_entity_introduced_by_set_provisional(db,
     entity_ident, commit_ident, commit_ts_iso)`, then
     `_candidate_diff_persist(db, commit_hash, entity_ident,
     _normalized_body_hash(node), commit_ts_iso)`.
   - **Fact exists and is provisional** → reverse walk has now reached an
     *earlier* commit where the same entity is still present, meaning its
     previous guess was too late. Call
     `_entity_introduced_by_set_provisional` again with `C`'s
     `commit_ident` (moves it earlier) and persist a fresh candidate-diff
     record for `(C, entity_ident)`. The now-stale candidate-diff record at
     the previously-guessed commit is intentionally left in place — 2c
     decides how to reconcile a superseded candidate when it reaches that
     commit chronologically.
   - **Fact exists and is authoritative** → do nothing to `:introduced-by`;
     only the `:modified-in` edge from step 1 applies.
3. **`:modified-in`**: always asserted for every entity touched by `C`,
   regardless of which branch above fired — this edge is order-independent
   and always authoritative per the issue's own design principle, so it
   never goes through the provisional marker.

### Driving functions

```python
def _reverse_fill_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """Claim exactly one position from the gap's high end and process that
    one commit per the algorithm above. Returns the claimed commit's hash,
    or None if the gap was already empty (allocator.claim_high() returned
    None) -- caller's signal to stop."""


def _reverse_bulk_fill_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    run_ts_iso: str,
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> int:
    """Repeatedly call _reverse_fill_claim_and_process, persisting each
    claim via _frontier_persist_claim, until the gap closes. Returns the
    count of commits processed. No caller in this sub-phase -- 2d wires
    this into the real concurrent ingestion loop."""
```

Both functions live in `mcp_server.py`, immediately after phase 2a's
existing functions. Neither is called by `_run_ingestion` in this
sub-phase.

## Testing

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), using
the same `tmp_path / "repo"` real-git-repo fixture pattern already used
throughout `tests/test_mcp_server.py`'s existing ingestion tests:

- **Single-commit claim**: a two-commit repo; claim once; assert the
  correct (newest) commit was claimed, structural facts + provisional
  `:introduced-by` + `:modified-in` were written, and the entity reads as
  provisional via `_lineage_is_provisional`.
- **Multi-commit walk moves the candidate earlier**: a function present
  across three commits `h0 < h1 < h2` (all touching the file); walk from
  `h2` down through `h0`; assert `:introduced-by` ends up pointing at `h0`
  (not `h2` or `h1`), only one live `:introduced-by` fact exists (raw count,
  not just the accessor — matching the phase 1/2a lesson on row-collapsing
  checks), and a candidate-diff record exists for `h0` (the final,
  correct guess).
- **Already-authoritative entity is left alone**: pre-seed an entity with a
  non-provisional `:introduced-by` (simulating Stream 1 having already
  confirmed it); run the reverse walk over a commit that touches it; assert
  `:introduced-by` is unchanged and no `:type/lineage-marker` companion
  entity was created, but a `:modified-in` edge was still added.
  `_entity_introduced_by_set_provisional` must be directly tested for this
  no-op-on-authoritative guard, not only indirectly through the walk.
- **Gap-empty no-op**: an allocator whose gap is already empty; assert
  `_reverse_fill_claim_and_process` returns `None` and writes nothing.
- **`_frontier_persist_claim` integration**: after `_reverse_bulk_fill_walk`
  processes N commits, assert the persisted frontier-high interval reflects
  N claimed positions (mirrors phase 1's own `TestFrontierPersistClaim`
  assertions).
- **Idempotency of `_entity_introduced_by_set_provisional`**: calling it
  twice with the same `commit_ident` results in exactly one live
  `:introduced-by` fact (raw count check).
