# #263 — `_code_ident` collision audit: design

Issue: project-minigraf/temporal_reasoning#263
Date: 2026-08-11
Status: design approved, not yet implemented

## What this measures

How many ident values produced by `_code_ident` (`mcp_server.py:4288`) over this
repository's real history are reachable from **more than one distinct input**?

`_canonical_ident` (`mcp_server.py:4090`) replaces every character outside
`[a-z0-9-]` with a hyphen and then collapses runs of hyphens. `_code_ident`
builds its input as `f"{file_path}::{name}"`, so the `::` separator and a
leading underscore in `name` both become hyphens and the collapse merges them:

```
tests/test_mcp_server.py::_commit  ->  tests-test-mcp-server-py---commit  ->  tests-test-mcp-server-py-commit
tests/test_mcp_server.py::commit   ->  tests-test-mcp-server-py--commit   ->  tests-test-mcp-server-py-commit
```

Two distinct code entities become one graph entity, and their lifecycles
interleave on it. The audit answers one question and nothing else: **how many,
and of what shape.** That number decides whether a fix needs a migration for
existing graphs or only a forward change.

This is the read-only-measurement-first precedent set by #244, #245 and #257.

## Why the graph cannot answer this

#263's body suggests the #257 census (`census_distinct_values` in
`evals/at_scale/probe_description_preload_exposure.py`) already computes a lower
bound for free, undercounting only because two colliding entities that share a
description would not show up. **The undercount is structurally worse than
that, and the audit must not be built on the graph.**

When two entities collide, the second one to be parsed hits the `ident in
entity_valid_from` branch in `_build_code_triples`. That branch appends a
`:modified-in` triple and nothing else — it writes no `:entity-type`, no
`:ident`, no `:description`, no `:file`, no `:introduced-by`. **The graph
therefore holds no record of the second entity's `(file_path, name)` pair at
all.** Widening the census key from `:description` to `(:file, :description)`
does not help, because the pair is simply absent.

The three collisions #257 found were visible only because those entities were
closed and later reopened, which re-runs the introduction branch and rewrites
`:description`. A collision whose loser never closes is invisible to any
graph-side query, no matter how it is keyed.

An exact count therefore has to be derived from the **inputs** — the
`(entity_type, file_path, name)` triples parsed out of source — not from the
facts that survived. Deriving from inputs makes the count exact rather than a
bound.

## The `:module/` namespace is shared by three producers

Discovered while writing this spec; it widens the scope agreed in
brainstorming and should be verified by the reader rather than taken on trust.

`external-dependency` is **not** a separate ident namespace.
`_preload_known_entities`' docstring states it (`mcp_server.py:7396`):
"external-dependency entities share the module ident namespace and use the same
`path` attribute as modules". Three call sites produce idents into that one
`:module/` namespace:

1. **In-tree modules** — `_code_ident("module", file_path)`
   (`mcp_server.py:7044`).
2. **Submodules / gitlinks** — `_code_ident("module", path)`
   (`mcp_server.py:9556`), written with `:entity-type :type/external-dependency`
   (`mcp_server.py:9563`).
3. **Unresolved imports** — `_canonical_ident("module", import_name)`, the
   fallback returns at `mcp_server.py:4250` and `4285`, written with
   `:entity-type :type/external-dependency` (`mcp_server.py:9441`).

Consequence for the audit: the module surface must be pooled into **one**
bucket across all three producers, not audited three times in isolation. A
cross-producer collision — an unresolved import slugging onto an in-tree
module's ident, or two distinct import specifiers (`foo_bar`, `foo-bar`,
`foo.bar`) landing on `:module/foo-bar` — is a real collision of the same kind,
and only a pooled grouping can see it.

## Scope decisions

**In scope.** All five `_code_ident` entity types — `module`, `function`,
`class`, `variable`, `field` — plus the unresolved-import producer that shares
the `module` namespace. (#263's scope note lists `external-dependency` as a
sixth type; it is not one, for the reason given in the section above.) These
are all derived from parsed source, so a single extraction pass covers them.

**Out of scope: `heuristic_extract`** (`mcp_server.py:6568`). It slugs
natural-language phrases out of memory text. Collapsing there is plausibly
*working as intended* — it dedups near-identical phrasings — and the values
have no `(file_path, name)` structure to group by, so a collision count there
would need separate interpretation before it meant anything. If it turns out to
matter, it is its own issue.

**Out of scope: any fix.** This audit changes no ident derivation, writes no
facts, and opens no `MiniGrafDb` handle. The single-handle invariant is
satisfied trivially rather than carefully.

**Out of scope: migration sizing against a real graph.** The input-side count
establishes whether existing graphs are corrupted at all. Measuring how much a
given graph actually lost would need a completed at-scale ingestion and is
deferred to whichever issue takes the fix.

## Architecture

Three stages, in one process, over one pass of history.

```
_git_commits(repo, None, branch)
        |
        v
  [ProcessPoolExecutor]  _extract_commit(repo, hash, ignore_patterns)   <- Stage 1
        |                         (read-only, stateless, no DB)
        v
  (entity_type, file_path, name) triples  +  unresolved import specifiers
        |
        +--> group by _code_ident        -> offenders, shapes             <- Stage 2
        |
        +--> group by each candidate rule -> residual, rename count       <- Stage 3
        |
        v
  results/263-ident-collision-census.json
```

## Stage 1 — triple collection, through the real extractor

The audit drives `_extract_commit(repo_path, commit_hash, ignore_patterns)`
(`mcp_server.py:8455`) rather than re-implementing parsing. That function is
already documented as read-only, stateless, DB-free, and safe across a process
boundary — it is precisely what `_run_ingestion`'s worker pool runs. Driving it
directly means the audit measures the code that actually produces idents in
production, not a reimplementation that could drift from it.

Commits come from `_git_commits(repo_path, None, branch)` (`mcp_server.py:4333`),
ignore patterns from `_load_ignore_patterns(repo_path)` (`mcp_server.py:4721`),
so the file set matches what ingestion would actually parse.

For each commit, over each `(status, file_path, extracted, precomputed,
old_path)` entry in `file_results` with status `A`/`M`/`R` (`D` entries carry
`extracted is None` and are skipped):

| source | triple |
|---|---|
| `file_path` | `("module", file_path, None)` |
| `extracted["functions"]` | `("function", file_path, fn_name)` |
| `extracted["classes"]` | `("class", file_path, cls_name)` |
| `extracted["globals"]` | `("variable", file_path, gvar_name)` |
| `extracted["fields"]` | `("field", file_path, f"{owning_class}.{field_name}")` |
| `precomputed["resolved_imports"]`, rows with `is_resolved` false | import specifier, into the pooled `module` bucket |

The `field` qualification and the category-to-`entity_type` mapping mirror
`_precompute_file_triples` (`mcp_server.py:7089-7122`) exactly. Where this spec
and that function disagree, that function is right and this spec is a bug.

Walking `A`/`M`/`R` files across every commit reaches every version of every
file that ever existed on the branch: each distinct blob at each path is
introduced by exactly one such entry. No separate initial-tree pass is needed.

Triples are accumulated into a set, so a name unchanged across 400 commits
costs one entry, not 400.

**Cost.** No DB, no ingestion, no fact writes. The hours the #245 and #257
probes needed went to DB writes and #239's per-ident `:introduced-by` point
queries, none of which this touches. Commits are dispatched to a
`ProcessPoolExecutor` exactly as `_run_ingestion` does (`mcp_server.py:10572`).

## Stage 2 — collision grouping and shape classification

Group the collected triples by the ident the **current** rule produces:

- code entities: `_code_ident(entity_type, file_path, name)`
- pooled module bucket: `_code_ident("module", file_path)` for in-tree files and
  gitlink paths, `_canonical_ident("module", import_name)` for unresolved
  imports

An **offender** is an ident whose input set has more than one member. Reported
per entity type, with every offender's input set listed verbatim — a nonzero
count escalates to fix design, and that work should start from the data rather
than re-deriving it by hand.

Each offender is classified by shape, from its input set:

| shape | test |
|---|---|
| `leading-underscore` | two inputs identical but for a leading `_` on the name |
| `case-only` | inputs equal under `casefold`, unequal otherwise |
| `separator-vs-path` | the `name` of one input appears as a trailing path segment of another's `file_path` — the case `_code_ident`'s docstring already anticipates |
| `cross-producer` | module-bucket only: inputs from two different producers |
| `other` | anything else |

Shapes are computed per offender over all pairs in its input set, so an offender
may carry more than one shape label. `other` is the interesting bucket: it is
where a collision nobody has predicted would show up.

## Stage 3 — candidate rule scoring

The triples are already in memory, so re-grouping them under a different
derivation is nearly free. Five rules are scored on **two** numbers each:

- **residual** — offenders remaining under that rule
- **renames** — idents whose value differs from what the current rule produces

Renames matter as much as residual: any change to derivation renames entities in
every existing graph, and that cost is what decides forward-fix versus
migration.

| | rule | change to `_canonical_ident` | expectation |
|---|---|---|---|
| R1 | keep underscores | charset becomes `[^a-z0-9_-]` | fixes `_foo`/`foo`; renames nearly every ident, since paths like `test_mcp_server.py` carry underscores |
| R2 | no hyphen collapse | drop `re.sub(r"-+", "-", slug)` | fixes it via separator arity (`py---commit` vs `py--commit`); renames every *named* entity but leaves most `module` idents alone |
| R3 | R1 + R2 | both of the above | maximal information preservation |
| R4 | hash suffix | append `-` + `sha256(value)` hex, first 8 | total distinctness including case; renames everything; costs ident readability |
| R5 | independent part slugs | slug `file_path` and `name` separately, join with a fixed token | **control — expected to still collide** |

**R5 is deliberately a rule expected to fail.** Slugging the name
independently still runs `strip("-")` over it, so `_commit` and `commit` both
reduce to `commit` and the collision survives the change. R5 exists so the
scorer has a known-negative to be checked against: **if the scorer reports R5
clean, the scorer is wrong**, and every other row it produced is suspect.

Rules are implemented as standalone pure functions in the probe module, not by
monkeypatching `mcp_server._canonical_ident`. The audit must not mutate the
module it is measuring.

## Predictions, fixed before any data exists

Recorded now so the run cannot be rationalized afterwards.

1. **The true collision count exceeds 3.** The #257 census found three, and it
   can only see collisions whose loser was closed and reopened. Any collision
   that never closed is invisible to it and visible here.
2. **`leading-underscore` is the dominant shape** on the function surface.
3. **R5 reports nonzero residual**, and its residual is at least the
   `leading-underscore` offender count.
4. **R2's rename count is far lower on `module` than on `function`.** Module
   idents have no `::` separator, so they only change when their path already
   contained adjacent non-alphanumerics.
5. **R4's rename count equals the total ident count.** Every ident gains a
   suffix.

A prediction that fails is a finding about this design, not noise to be
smoothed over. It goes in the recorded result.

## Report fields

Written to `evals/at_scale/results/263-ident-collision-census.json`:

- `repo_path`, `branch`, `head_commit`, `commits` — provenance, with
  `head_commit` taken from the walked commit list rather than a separate
  `git rev-parse`, so it cannot disagree with what was measured
- `triples_total`, `idents_total` — per entity type and pooled
- `extraction_failures` — commits `_extract_commit` raised on, with hashes
- `offenders` — per entity type: count, and the full ident → input-set map
- `offenders_by_shape` — counts per shape label
- `candidates` — per rule: `residual`, `renames`, and the rule's own
  description string
- `predictions` — each prediction above, with its outcome recorded as
  `held` / `failed` and the number that decided it
- `timestamp`

## Exit code

**Validity-only**, following the #257 probe's precedent. Nonzero exit means the
*run* was invalid, never that collisions were found — finding them is the
expected result of an audit whose entire purpose is to count them.

Exit nonzero when: zero commits were walked; zero triples were collected; or
`extraction_failures` exceeds 1% of commits walked.

## Testing

`tests/test_at_scale_ident_collision_census.py`, alongside the existing
`test_at_scale_dep_preload_probe.py` and `test_at_scale_description_preload_probe.py`.

Grouping, shape classification and rule scoring are pure functions over
triples, so they are tested directly without walking a repository.

**Ablation requirement.** Per the standing rule that a regression test must be
shown to actually fail against the behaviour it claims to guard, every test
below states the counterfactual it was checked against:

1. **The three known real collisions as a fixture.** The `(file_path, name)`
   pairs behind `:function/tests-test-mcp-server-py-commit`,
   `…-snapshot` and
   `:function/evals-at-scale-profile-forward-reconcile-attribution-py-main`
   must group as offenders with shape `leading-underscore`.
   *Counterfactual:* feeding only the public member of each pair yields zero
   offenders.
2. **R5 reports these same three as residual.** This is the scorer's
   known-negative.
   *Counterfactual:* a scorer that returns zero residual unconditionally passes
   test 1 and fails this one.
3. **R1, R2 and R3 each report them clean, and each reports a nonzero rename
   count.** A rule that renames nothing cannot have changed derivation.
4. **Shape classification separates its labels.** A case-only pair is not
   labelled `leading-underscore`; a `separator-vs-path` pair is not labelled
   `other`.
   *Counterfactual:* a classifier returning a constant label fails.
5. **Cross-producer pooling.** An unresolved import specifier and an in-tree
   file path that slug to the same `:module/` ident are reported as one
   offender with shape `cross-producer`.
   *Counterfactual:* auditing the two producers into separate buckets reports
   zero.
6. **End-to-end through real extraction.** A purpose-built temp git repo with a
   single Python file containing both `def _foo` and `def foo` is walked by the
   real `_extract_commit`, and the collision reproduces. This is the test that
   catches the audit mis-driving the extractor — every test above would still
   pass if Stage 1 collected the wrong triples.

## Deliverables

- `evals/at_scale/probe_ident_collision_census.py` — the audit, with a CLI
- `tests/test_at_scale_ident_collision_census.py` — the tests above
- `evals/at_scale/results/263-ident-collision-census.json` — the recorded run
- a note in `evals/at_scale/benchmark.md` placing this among the one-off
  read-only probes, matching how `probe_dep_preload_exposure.py` is described
- `CLAUDE.md` needs no change: `evals/at_scale/` is already described as
  holding one-off read-only probes and their recorded measurements

## What this explicitly does NOT do

- **It does not fix anything.** No change to `_canonical_ident`, `_code_ident`,
  or any caller.
- **It does not propose a migration.** It produces the number that decides
  whether one is needed.
- **It does not measure an existing graph.** See the scope decision above.
- **It does not audit `heuristic_extract`.**
- **It does not claim its candidate scoring is a fix recommendation.** Residual
  and rename counts are inputs to that decision, not the decision. A rule with
  zero residual may still be unacceptable on rename cost, and choosing among
  them is fact-model work with its own schema, idempotency, migration-coverage
  and transact-path checks to clear.
