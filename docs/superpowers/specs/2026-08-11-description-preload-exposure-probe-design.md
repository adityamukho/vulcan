# #257 — `:description` preload exposure probe: design

Issue: project-minigraf/temporal_reasoning#257
Date: 2026-08-11
Status: design approved, not yet implemented

## What this measures

`_preload_known_entities` decides preload MEMBERSHIP by position after #238 and
#245 (PR #258). It seeds `entity_descriptions[ident]` from whichever
`:description` fact version was live at `:valid-at T_hi(W)` — a DATE query.
`T_hi(W) = max(ts[0..W])` is the monotone envelope of author dates at or below
the watermark, and author dates are not monotonic in topological order, so a
version written by a commit ABOVE the watermark but carrying an EARLIER author
date can be the version that query returns.

#257 asks how often that actually happens on real history. It is UNMEASURED:
the #245 probe (`evals/at_scale/probe_dep_preload_exposure.py`) compared
membership only.

## The issue's stated mechanism does not match the code

Three checks against master `1dceec1`, all of which the reader should
re-verify rather than take from this document:

1. **`:description` is written ONCE per entity lifetime**, not "on essentially
   every body edit" as #257's body claims. `_build_code_triples`' docstring
   states it (`mcp_server.py:7182-7186`) and the code matches: every
   `entity_descriptions[ident] = …` assignment (7228, 7243, 7257, 7271, 7283)
   sits inside the `if ident not in entity_valid_from` introduction branch. The
   `elif` branch for an already-known entity appends a `:modified-in` triple and
   nothing else. The "per-attribute interval history is large" premise — #257's
   stated reason for scoping this out of #238/#245 — is therefore false.

2. **Nothing uses `entity_descriptions` for body-change detection.** Every read
   in `_forward_apply` (9216, 9254, 9396, 9464, 9507, 9583) feeds
   `_build_close_triples`' `desc` argument, i.e. the RETRACT VALUE at close
   time. Body-change detection is `unchanged_idents`, computed in
   `_precompute_file_triples` from parsed node text (#221). The failure #257
   describes — "the comparison reports unchanged and the edit is never
   recorded" — has no code path.

3. **For five of the six preloaded entity types the value is a deterministic
   function of the ident.** module → `file_path`; function/class/variable →
   name; field → `qualified_name`. `_code_ident(entity_type, file_path, name)`
   (mcp_server.py:4288) is built from exactly those inputs. A `:description`
   version written above the watermark therefore carries the SAME string, so
   the date-versus-position choice cannot change the value returned.

Two places where the value is genuinely NOT ident-determined:

- **submodule external-dependency**: `description = name or path`, read from
  `.gitmodules` (mcp_server.py:9546). The name can change while the path, and
  so the ident, stays fixed.
- **`_canonical_ident` slug collisions**: `_code_ident`'s own docstring calls
  the ident best-effort and says collisions remain possible for contrived
  path/name combinations, so two distinct names could share one ident while
  carrying different descriptions.

**The real consequence, if the mechanism ever fires, is different from the one
#257 describes.** A wrong preloaded `desc` becomes a retract value that does not
match the stored fact, so the `:description` fails to close and stays live past
its window. That is a stale-fact bug, not a lost body edit.

This spec does not act on the above as settled. It is the reason the work is
scoped as a measurement, and it is what the measurement is expected to confirm
or refute.

## Prediction, fixed before any data exists

Following the #239 discipline of fixing the threshold in the spec before
running anything:

> **Census zero. Mismatches zero.**

on the argument in the section above. A nonzero census is the falsifier. If it
fires, the work escalates to the full per-position value sweep described in
#257's "How to measure it", which Stage 2 below already implements.

Recording the prediction here is what keeps a zero result from being read as
"the probe was built to return zero".

## Scope decisions

- **Probe first, fix never (yet).** No interval-inversion machinery is built.
  The measurement decides whether any is justified, per the project's standing
  rule that a real code path is not a real problem until measured.
- **The submodule arm is reported UNMEASURABLE, not zero.** This repository
  produced 0 gitlink events when #245 measured it over 610 commits, so no
  submodule `:description` facts exist to count. The probe recomputes the count
  rather than assuming it. #245 recorded `:pinned-commit` the same way, and that wording is
  reused verbatim. A zero census therefore reads "zero on the five
  ident-determined types, unmeasurable on submodules" and is not by itself
  sufficient to close #257 — the residual must be named explicitly in whatever
  close is written.
- **Step 4 of #257's sketch is dropped.** It is conditional on step 3 being
  nonzero, and it asks to replay a body-change comparison against
  `entity_descriptions` that does not exist. If step 3 fires, the correct
  follow-up is to check whether the retract value matched, not whether a body
  edit was suppressed.

## Architecture

New module `evals/at_scale/probe_description_preload_exposure.py`. It imports
the pure analysis primitives from `probe_dep_preload_exposure`:
`VALID_TIME_FOREVER_MS`, `build_ts_positions`, `resume_envelopes`,
`affected_positions`, `invert_ms_to_positions`, `edge_live_at`,
`gitlink_event_count`. That module's docstring already commits to those staying
importable without opening a graph.

Separate module rather than a `--mode` flag on the existing probe:
`probe_dep_preload_exposure.py` is 1128 lines organised around a NARROW/WIDE
entity framing with no analogue here, and CLAUDE.md already describes
`evals/at_scale/` as holding one-off read-only probes alongside the recurring
benchmark.

### Obtaining a graph

Default: temp-dir ingest via `probe_dep_preload_exposure._ingest_into`,
refusing to proceed unless `ingest_status == "complete"` — `_run_ingestion`
never raises on failure, so that status is the only signal the graph underneath
is whole.

Optional `--graph-path` runs against an existing persistent graph, gated on the
graph's commit count matching the linearization length so a partial graph
cannot be swept silently. This exists because a ~30-minute ingest per iteration
would otherwise be the entire cost of the work. It overlaps #256's want for a
persistent-graph option and is deliberately kept minimal here: read an existing
graph or refuse, no ingest-if-absent behaviour.

**Single-handle invariant** (CLAUDE.md): at most one live `MiniGrafDb` per
process. Follow `probe_dep_preload_exposure.main`'s pattern exactly — reuse
`mcp_server._db` when it is already set, and open one only when it is not.

## Stage 1 — the distinct-value census

Six queries, one per entity type `_preload_known_entities` loads (`module`,
`function`, `class`, `variable`, `field`, `external-dependency`):

```
[:find ?ident ?desc ?vf ?vt :any-valid-time
 :where [?e :entity-type :type/<T>]
        [?e :ident ?ident]
        [?e :description ?desc]
        [?e :db/valid-from ?vf]
        [?e :db/valid-to ?vt]]
```

**Clause order is load-bearing.** `[?e :description ?desc]` MUST be the EAV
clause immediately preceding the two pseudo-attributes: they bind to whichever
EAV clause on `?e` most recently precedes them, so putting `[?e :ident ?ident]`
between them would bind `?vf`/`?vt` to the `:ident` fact's window instead —
wrong, and silently so. This is `load_module_path_facts`' documented rule,
applied to `:description`.

Rows dedupe on `(ident, desc, vf, vt)`. An entity carrying several
`:entity-type` or `:ident` versions across time otherwise multiplies each
`:description` row under `:any-valid-time` without changing any answer, exactly
as `load_dep_edges` dedupes for the same reason.

Census output, per entity type: total distinct idents, count of idents carrying
more than one distinct `:description` VALUE (not more than one interval — an
entity deleted and re-added has two intervals and one value, and that is not
exposure), and the offending `(ident, sorted values)` pairs verbatim so a
nonzero result is diagnosable rather than merely counted.

Reported alongside `gitlink_event_count(repo_path)`, with a zero submodule
population labelled UNMEASURABLE.

## Stage 2 — the position-correct value diff

**One mode only.** Unlike the #245 probe's date-only/`--verify-fix` pair, #257
is the residual in the SHIPPED code, so `_preload_known_entities` is always
driven with the full post-#238/#245 argument set: `valid_at=envelopes[w]`,
`hash_to_pos`, `watermark_pos=w`, `ts_positions`, and
`t_hi_ms=_iso_to_epoch_ms(envelopes[w])`. There is no pre-fix leg to measure.

For each `w` in `affected_positions(commit_metadata)`:

1. **Oracle** — for each ident, the set of DISTINCT `:description` values whose
   own fact interval is live at `w`, computed by running that fact's `vf_ms` and
   `vt_ms` through `invert_ms_to_positions` and `edge_live_at`. Both ends
   inverted to positions; no date bound anywhere in the oracle.
2. **Actual** — the `entity_descriptions` dict `_preload_known_entities`
   returns at that `w`.
3. **Finding** — `value_mismatch` where an ident's preloaded value is not a
   member of the oracle's live-value set for that ident. Reported both
   position-weighted (one ident mismatching at all N affected positions counts
   N) and as distinct idents (the union across positions), matching every #245
   counter's convention.

Three quantities are counted separately and deliberately EXCLUDED from the
finding:

- **`ambiguous_idents`** — the oracle's live-value set has more than one member,
  so no single correct value exists to compare against. `edge_live_at`'s
  asymmetric collision policy exists to avoid understating a MEMBERSHIP answer;
  applied to a value it would fabricate one. Counted, never resolved.
- **`preloaded_not_live`** and **`live_not_preloaded`** — membership
  disagreements between the preload and the oracle. This is #238/#245's
  already-measured, already-fixed territory. Folding it into #257's number
  would re-measure a closed issue and inflate this one. Diagnostics only.

## Report fields

Provenance (the #245 probe added these after its first artifact named a scratch
directory that no longer existed): `repo_path`, `branch`, `head_commit`,
`commits`, `ingest_status`, `affected_positions`.

Finding: `census_idents_with_multiple_values` (per type and total),
`census_offending_idents`, `value_mismatch_total_position_weighted`,
`value_mismatch_distinct_idents`, `ambiguous_idents_total`,
`preloaded_not_live_total`, `live_not_preloaded_total`, `gitlink_events`.

Validity: `unmappable_description_valid_from`, `unmappable_description_valid_to`,
`timestamp_collisions`, `preload_descriptions_empty_everywhere`.

`preload_descriptions_empty_everywhere` is a lie-detector, not a statistic.
`_collect` swallows its own query failure with a bare `except Exception: pass`
per entity type (`mcp_server.py:7491-7492`), so "this history genuinely has no
descriptions" and "every query failed" produce identical mismatch counts. The
report records the actual per-position `entity_descriptions` size and flags the
all-positions-empty case explicitly, mirroring what #245 built for
`_preload_known_deps`.

## Exit code

**The exit code reflects measurement VALIDITY, not the finding.** A nonzero
mismatch count is the number #257 asks for; it must not fail the run.

Exit 1 iff `unmappable_description_valid_from > 0`, or
`unmappable_description_valid_to > 0`, or `timestamp_collisions > 0`.

Unmappable facts mean the timestamp-to-position inversion the whole oracle
rests on is broken for at least one fact. Nonzero collisions belong in the same
gate rather than merely widening error bars, because the shipped
`_position_of_valid_time` and the oracle's `edge_live_at` resolve a collision in
OPPOSITE directions — a collision makes the comparison invalid, not just noisy.
The #245 acceptance run recorded 0 collisions on this repository, so this gate
is expected to pass.

## Testing

`tests/test_at_scale_description_preload_probe.py`, mirroring
`tests/test_at_scale_dep_preload_probe.py`. Synthetic fact sets only — the
primitives are pure, so no graph is opened:

1. The oracle rejects a `:description` version introduced ABOVE `w` carrying an
   EARLIER author date, and selects the version live at `w` — the #257 shape
   itself.
2. An ident whose oracle live-value set has more than one member is counted in
   `ambiguous_idents` and contributes nothing to `value_mismatch`.
3. An ident present in the preload but absent from the oracle's live set (and
   vice versa) contributes to the membership diagnostics and nothing to
   `value_mismatch`.
4. A `:description` fact whose `vf` or non-sentinel `vt` inverts to no position
   is counted in the unmappable diagnostics rather than skipped.
5. The census counts distinct VALUES per ident, so an entity with two intervals
   carrying one value is not counted as exposure, while one interval pair
   carrying two values is.

**Every one of these must be ablation-proven** before it is accepted: each test
must be shown to FAIL against a date-bounded oracle, with the counterfactual
matching the real date-bounded code rather than a convenient restatement of it.
This is the standing rule after four tests on an earlier branch claimed
guarantees they did not provide.

## Deliverables

- `evals/at_scale/probe_description_preload_exposure.py`
- `tests/test_at_scale_description_preload_probe.py`
- `evals/at_scale/results/257-description-preload-exposure.json` — the recorded
  measurement, committed as an analysis artifact the way #239's and #245's were.
- A comment on #257 carrying the verdict AND correcting the two false premises
  in its body (written-once, and the nonexistent body-change consumer), so the
  record does not keep pointing at a mechanism that has no code path.

## What this explicitly does NOT do

- Build the per-attribute position-indexed interval reconstruction #257
  sketches as the eventual fix. That is justified only by a nonzero
  measurement.
- Change any behaviour in `mcp_server.py`. The probe is read-only over an
  already-ingested graph.
- Close #257. Whether a zero result closes it is a judgement call, and the
  unmeasurable submodule arm has to be named in whatever close is written.
