# #263 — R3 ident rule + graph format version: design

Fixes the ident collisions measured by the #263 audit (PR #264) by changing the
slug rule, and replaces the migration that change would otherwise require with a
graph-level format version and a loud refusal.

## The decision this implements, and why there is no migration

The audit found **9 of 2780 idents (0.32%) reachable from more than one
`(entity_type, file_path, name)` input** over 674 commits. It also priced the
repair: the cheapest zero-residual rule renames **97.5% of every ident in every
existing graph**, so a fix needs a migration that rewrites every fact whose
subject *or* object is an old ident, across bi-temporal history.

That migration is **not being built.** The user's standing decision (2026-08-14)
is that **any graph built before #222 closes must be fully rebuilt into a fresh
graph path, never migrated or re-ingested in place** — there is no graph worth
preserving and no other active user. That is a superset of this change: several
#222-arc defects (#235's two-value `:introduced-by`, #251/#253's page-table
corruption, #238/#245's misclassified `:depends-on`, phase 2d's data loss) write
wrong facts and do **not** self-heal on a later ingest, so those graphs were
already condemned independently of #263.

Consequence: the rename cost is zero, and this is the cheapest moment this fix
will ever have. It gets more expensive the moment a graph exists that someone
would rather keep.

## Rule: R3

R3, verbatim from the audit's `_slug_keep_underscore_no_collapse`:

```python
re.sub(r"[^a-z0-9_-]", "-", value.lower()).strip("-")
```

Two changes from the current slug: `_` joins the allowed charset (so a private
marker survives), and the consecutive-hyphen collapse is dropped (so separator
arity carries information — `py--commit` against `py---commit`).

Scored by the audit at **residual 0, renames 2721 of 2780**. Re-derived
independently for this spec against the census artifact's own offender list: all
9 pairs separate. That check is the acceptance corpus below, not a citation of
the audit's score.

**R4 (hash suffix) was rejected**, and the reason changed once the migration
died. R4's appeal was total distinctness *by construction* when a migration was
being paid for anyway. With nothing to preserve, the trade is judged on merits,
and the merit that matters is that idents are the human- and agent-legible
handle in query results: `:function/mcp_server-py--run_ingestion` reads,
`:function/mcp-server-py-run-ingestion-a3f9` does not.

**R3's zero is MEASURED over 674 commits of this repo, not proven by
construction.** That is a real difference from R4 and is the price of the
choice. It is why the census probe is retained as a gate (below) rather than
retired as a one-off.

### Verified: minigraf accepts these idents

The one unknown that could have sunk R3. Confirmed empirically against
minigraf 1.2.3 before this spec was written: `_` and hyphen-runs are accepted
inside a keyword ident, round-trip on point queries, and
`:function/mcp_server-py--_foo` / `--foo` and `--__init__` / `--_init` resolve
to distinct entities.

## The slug changes GLOBALLY, in `_canonical_ident`

Not only in `_code_ident`. `_canonical_ident` (`mcp_server.py:4090`) is the
single slug function; `_code_ident` (`4288`) delegates to it.

The `:module/` namespace has three producers, and the one non-underscore
collision does not go through `_code_ident` at all. Per the census artifact,
`:module/mcp-server` is reached from **two unresolved import specifiers** —
`mcp.server` and `mcp_server`, both `producer: "import"` — which reach the
namespace via `_resolve_module_ident`'s bare `_canonical_ident` calls
(`4250`/`4285`), because `.` and `_` both slug to `-`. Changing only
`_code_ident` would leave this pair colliding untouched.

(The audit write-up glossed this as "external dependency colliding with an
in-tree module name". The census's own `producer` field says both sides are
import specifiers; the failure mode is the same but the path is not
`_code_ident`, which is exactly why the fix must live in `_canonical_ident`.)

Going global also re-slugs the memory-entity idents minted at `6568`
(`:decision/`, `:preference/`, `:constraint/`, …). Underscores are rare in
natural-language-derived values so the practical effect is small, and with no
graph worth preserving the cost is zero. One slug rule is also simpler to
document and to reason about than two.

## Graph format version — replacing the migration with detection

The failure this closes is **silent split-brain**, and it is a direct
consequence of how idents work: `_code_ident` is a **pure function** of
`(entity_type, file_path, name)`, recomputed at every call site on every commit
of every run. There is no stored ident table that ingestion consults. So an
old-rule graph ingested by new-rule code does not error — it forks every
entity. Membership checks key on the ident string, so each entity reads as brand
new: `:introduced-by` re-set at the wrong commit, old facts never closed,
`:contains` / `:modified-in` / `:depends-on` / `:class` split across two idents.

### Shape

A single `:ingestion/format-version` entity carrying `:version` (int).

**`ingestion` is a REGISTERED `MINIGRAF_SCHEMA` type** (required
`{:description}`; optional `{:hash, :alias, :last-run-at, :last-commit,
:total-ingested}`). `minigraf_audit` iterates registered types and retracts any
attribute outside a type's allowed set, querying the live graph directly — so a
new attribute that is not added to that set is **silently destroyed by any audit
run**. `:version` is therefore added to `ingestion`'s optional set, and
`:description` is written because it is required.

This is check 1 of the four schema/idempotency checks; all four apply:

2. **Deterministic ident ≠ idempotent write.** The write must read the current
   value first, no-op if unchanged, retract-then-reassert only if it genuinely
   differs — the guard `_watermark_update` already establishes. A test must call
   it twice and assert the raw fact count stays at one, and must prove
   non-clobbering of an *advanced* value, not just non-duplication of an
   unchanged one.
3. **Absent stamp means version 0, NOT "fresh".** This is the trap that decides
   whether the guard works at all. Every graph that exists today predates the
   stamp, so "no stamp" must read as *old*, never as *new*. A guard that treats
   absence as "fresh, adopt current version" silently stamps a corrupt graph as
   good and produces exactly the split-brain it was built to prevent. The stamp
   is written only when the graph is genuinely empty of ingestion state.
4. **Internal `_transact`, not the public handler.** `ingestion` is registered
   so `handle_minigraf_transact`'s `_validate_facts` would accept it, but the
   established pattern for ingestion-internal metadata is the internal helpers
   (`_transact`/`_retract`), as `_watermark_update`/`_frontier_persist_claim`
   already do. Follow it.

### Refusal, and why the check and the write are two functions

On ingest, a stamp that does not match the current version **refuses before any
write** — not partway through a run — naming the fix: rebuild into a fresh graph
path. Refusing mid-run would leave a graph half-written under two rules, the
failure this exists to prevent.

This forced a split the spec did not originally anticipate, driven by two
constraints that cannot both be met by one function:

- `_graph_format_version_verify(db)` is **read-only** and runs at the very top
  of `_load_ingestion_preload_state`, the earliest point in a run holding a db.
- `_graph_format_version_stamp_if_new(db, ts, index_con)` is the run's **first
  write**, on the write path with the run's `index_con`.

The `index_con` is not incidental. A write without it falls through
`_index_write`'s `index_con=None` path, which opens and commits a fact-index
connection of its own — caught by
`test_ingestion_commits_index_once_per_commit_not_per_triple`, which pins
exactly one `open_writer` per run.

The stamp must also be the first write rather than the last: a fresh graph that
got its ingestion state written but not its stamp would, on the next run, be
indistinguishable from a pre-#263 graph (state present, stamp absent) and be
refused. `_graph_format_version_stamp_if_new` therefore also refuses to stamp a
graph that already carries ingestion state, as a backstop to that ordering.

## Regression gate: the 9 pairs, not the full probe

Two different things, deliberately separated.

- **CI gate (new unit test):** the 9 offender pairs from the census artifact,
  pinned as data, asserted to produce distinct idents under the shipped
  `_code_ident`/`_canonical_ident`. Cheap, deterministic, runs in the suite.
  This is what catches a regression.
- **`probe_ident_collision_census.py` is FROZEN at the pre-#263 rule.** This
  reverses what this spec first said (that it would be retained as a live
  re-measurement tool), and the reason is worth recording because it was not
  obvious until the build: **its `PREDICTIONS` block was registered before any
  data existed**, and P3/P4 are claims about R5 and R2 *as measured against the
  old baseline*. Re-pointing its baseline at production would silently
  re-evaluate pre-registered predictions against a different experiment while
  still printing them as "held" — destroying the one property that makes a
  pre-registered prediction worth anything.

  So `current_ident` stops calling `mcp_server._code_ident` and composes the
  frozen slug instead, and the parity test inverts: the copy must equal the
  frozen historical rule and must **not** equal production. The probe now
  reproduces the audit forever, and a census of *new* history under the shipped
  rule needs its own probe — filed as a follow-up, not a mutation of this one.

Per the #261 precedent this gate asserts a count, not a duration, and needs a
positive control: the same 9 pairs must be shown to collide under the *old*
slug, or the test cannot distinguish "R3 separates them" from "the corpus was
wrong". The old slug is inlined in the test as the control.

## Testing

- The 9 offender pairs separate under the shipped ident functions; the same
  corpus collides under the inlined pre-R3 slug (positive control).
- R3 slug unit tests: underscore survives, hyphen runs preserved, leading and
  trailing hyphens still stripped, underscores NOT stripped at the edges.
- `_resolve_module_ident`'s external-import path and `_code_ident`'s in-tree
  path no longer meet on `mcp.server` / `mcp_server`.
- Format version: fresh graph gets stamped; absent stamp on a graph with
  ingestion state reads as version 0 and refuses; matching version proceeds;
  mismatched version refuses before any write; the write is idempotent across
  two calls and does not clobber an advanced value.
- `minigraf_audit` does not retract `:version` from the stamp entity.

## Docs sync

- **`SKILL.md:109`** currently states "Canonical ident form: lowercase, hyphens
  only — `:decision/redis` not `:decision/Redis_cache`." That is wrong under R3
  and must change: underscores are preserved, so `Redis_cache` slugs to
  `redis_cache`. This line is agent-facing guidance for minting idents by hand,
  so leaving it stale would actively teach the wrong convention.
- The code-entity ident conventions (`SKILL.md:378`, `388`, `398`, `425`, `435`)
  describe the `::`-join and namespace sharing, which are unchanged, but any
  worked ident examples must be re-slugged.
- `CLAUDE.md` needs the rebuild-not-migrate posture and the format version, so a
  future session does not propose a migration.

## What this explicitly does NOT do

- **No migration, and no repair of any existing graph.** Rebuild into a fresh
  path is the only supported recovery. This is the whole point, not an omission.
- **#257 stays open.** Its remaining scope is downstream of this
  (submodule `.gitmodules` renames become the sole remaining source of two
  distinct `:description` values), but that arm is unmeasurable on this repo —
  0 gitlink events — so this change makes #257 decidable, not decided.
- **Does not claim collision-freedom by construction.** R3's zero is measured.
  A contrived path/name combination could still collide; the gate and the probe
  are how that stays visible.
