# Provisional/Authoritative Lineage Fact Model + Candidate-Diff Persistence - Spec Review

**Reviewed spec:** `docs/superpowers/specs/2026-07-24-lineage-provisional-fact-model-design.md`
**Review date:** 2026-07-24

Memory had no recorded prior facts for lineage/provisional/candidate-diff, so this review is grounded in the spec and current repo code.

## Findings

### High: `:lineage-status` will make audited code entities invalid

The spec adds `:lineage-status` directly to code entities, but `MINIGRAF_SCHEMA` does not allow that attribute for `module`, `function`, `class`, `variable`, or `field`, and `minigraf_audit` is closed-world. I confirmed with an in-memory probe that audit flags such an entity.

Either add schema support for this attribute across code entity types, or store lineage state in separate `:lineage/...` entities.

References:
- Spec: `2026-07-24-lineage-provisional-fact-model-design.md:64`
- Code: `mcp_server.py:5083`

### High: Deterministic candidate-diff idents are not idempotent under current Minigraf valid-time behavior

The candidate-diff section says the deterministic ident is "naturally idempotent" if a caller revisits the same `(commit, entity)` pair. That is false with today's write semantics: re-transacting the same EAV at a new `valid_from` creates duplicate live rows. The repo already documents this in `_checkpoint_after_write` / `_watermark_update`.

`_lineage_mark_provisional` and `_candidate_diff_persist` need query-before-write guards or retract/reassert semantics. Tests should call them twice with different timestamps and assert raw counts stay at one.

References:
- Spec: `2026-07-24-lineage-provisional-fact-model-design.md:140`
- Code: `mcp_server.py:3295`

### Medium: `lineage-confirmed-through` is underspecified as a trust predicate

The spec describes `lineage-confirmed-through` as a cheap answer to "is region X fully confirmed," but it also says the watermark remains unset for migrated `[C0, W]` authoritative history. That makes the watermark insufficient as a standalone trust predicate.

Define the composed rule consumers must use, or seed/update the watermark to the migrated authoritative boundary.

References:
- Spec: `2026-07-24-lineage-provisional-fact-model-design.md:46`
- Spec: `2026-07-24-lineage-provisional-fact-model-design.md:115`

### Medium: Candidate-diff records need an explicit schema/audit decision

Candidate-diff records use `:candidate/...` idents with `:entity-type :type/candidate-diff`, but current validation and audit conventions assume aligned namespace/type names.

If these are schema-managed facts, align the namespace/type and specify required attrs such as `:description` and `:ident`. If they are intentional scratch/internal facts, state that explicitly and test audit interaction.

References:
- Spec: `2026-07-24-lineage-provisional-fact-model-design.md:126`
- Code: `mcp_server.py:3728`
