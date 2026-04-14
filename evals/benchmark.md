# Skill Benchmark: temporal-reasoning

**Date**: 2026-04-14  
**Model**: claude-sonnet-4-6  
**Iterations**: 2 (iteration-1 baseline evals → iteration-2 hardened evals)

## Summary

| Metric | With Skill | Without Skill | Delta |
|--------|-----------|---------------|-------|
| Pass Rate | **100%** (19/19) | **0%** (0/19) | **+1.00** |

All four evals pass with the skill. All four fail without it. The delta is maximally discriminating.

## Evals

### Eval 1 — Decision Storage

**Prompt**: User shares three architectural decisions (PostgreSQL 15, Redis session cache, FastAPI).  
**What it tests**: Does the skill cause Claude to persist decisions immediately, with correct naming convention and a meaningful reason?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 6/6 | 0/6 |
| Tool calls | 3× transact + 1× query | 0 |
| Key behavior | Stores PostgreSQL, Redis, FastAPI with `:project/entity/attribute` naming and per-decision reasons | Acknowledges conversationally; nothing persisted |

### Eval 2 — Populated Memory Retrieval

**Prompt**: "I can't remember — what database are we using? And the auth caching approach?"  
**Setup**: Memory pre-seeded with PostgreSQL 15, Redis (24h TTL), FastAPI decisions from a prior session.  
**What it tests**: Does the skill cause Claude to query memory and cite stored facts — not guess or refuse?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 | 0/5 |
| Key behavior | Queries, retrieves PostgreSQL 15 + Redis 24h TTL, cites both explicitly | Says "I don't have access to prior conversations" — memory is populated but never queried |

> **Why this eval matters**: The facts exist in memory in both runs. The skill is what makes them visible.

### Eval 3 — Cross-Session Preference Enforcement

**Prompt**: "Can you add a test for the user registration endpoint? Make sure it fits with how we do things."  
**Setup**: Memory pre-seeded (from a "previous session") with preference: no mocks in DB tests.  
**What it tests**: Does the skill cause Claude to discover and apply a constraint it was never told about in this conversation?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/4 | 0/4 |
| Key behavior | Queries memory, finds no-mocks preference, writes test using real DB connections | Writes test using `AsyncMock` — silently violates stored preference it never knew to check |

> **This is the strongest demonstration of the skill's value.** The prompt doesn't mention mocks. Claude must discover the constraint entirely from memory.

### Eval 4 — Conflict Detection

**Prompt**: "We need to connect to a MySQL database for a new analytics sidecar. Can you write the SQLAlchemy setup?"  
**Setup**: Memory pre-seeded with PostgreSQL 15 as the finalized primary database.  
**What it tests**: Does the skill cause Claude to surface a potential architectural conflict before silently switching databases?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/4 | 0/4 |
| Key behavior | Queries memory, detects PostgreSQL decision, flags the conflict, asks for clarification before proceeding | Writes complete MySQL connection setup with no mention of the existing PostgreSQL decision |

> Without the skill, architectural consistency can be silently broken in a single prompt.

## Observations

- **Eval 3 is the most discriminating**: it tests cross-session retrieval of an *implicit* constraint — the prompt gives no hint that a relevant preference exists. Only memory makes it visible.
- **Eval 4 demonstrates harm prevention**: the baseline isn't merely unhelpful, it's actively dangerous — silently overriding an architectural decision with no flag.
- **The baseline never hallucinates**: it either refuses to answer (eval 2) or fulfills the request without checking memory (evals 3, 4). The skill doesn't prevent bad answers — it enables informed ones.
