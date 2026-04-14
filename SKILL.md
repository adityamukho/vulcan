---
name: temporal-reasoning
description: >
  Use this skill whenever the user mentions decisions ("we'll use X", "going with Y", "decided to Z"),
  preferences ("I prefer", "I don't like", "always use", "never use"), constraints ("must be", "can't use",
  "prioritize"), dependencies ("depends on", "requires"), or references past context ("what did we",
  "last time", "before", "earlier", "what was our"). Also use before any code modification that might
  conflict with past decisions — if you're about to touch an area where architectural choices might apply,
  query first. When in doubt, query.
---

# Temporal Reasoning Skill

Persistent bi-temporal graph memory for AI coding agents. Stores architecture decisions, preferences, constraints, and dependencies so they survive across sessions — preventing the context drift that causes repeated questions, contradictory advice, and violations of established patterns.

## The Core Idea

Without memory, every conversation starts from zero. You end up asking the user things they've already answered, writing code that contradicts decisions they've already made, and missing constraints they told you about weeks ago. This skill gives you a persistent store you can write to and query at any time.

**The two habits this skill builds:**
- **Write immediately** when the user establishes something worth keeping (decision, preference, constraint)
- **Read before acting** when the user asks about the past, or when you're about to modify something where past decisions might apply

## When to Write (minigraf_transact)

Write to memory when the user's words signal a durable fact:

| Signal | Examples | What to store |
|--------|----------|--------------|
| Decision language | "we'll use X", "going with Y", "we decided Z" | The decision + what was rejected |
| Preference | "I prefer", "I don't like", "always/never use" | The preference + why (if given) |
| Constraint | "must be", "can't use", "prioritize X over Y" | The constraint + the tradeoff |
| Dependency | "depends on", "requires", "calls into" | The relationship |
| Architecture | system structure, component roles, data flows | The structure + rationale |

Store the *why* when you have it — a reason like "chosen for async support" is far more useful than the bare fact "using FastAPI".

After every write, say: "I've stored that in memory." and summarize what was stored.

## When to Read (minigraf_query)

Query memory before you answer or act, when:
- The user asks about past decisions, architecture, preferences, or constraints
- The user says "what did we...", "how did we...", "why did we...", "what was our..."
- The user references something from "earlier", "before", "last time"
- You're about to write code that touches existing architecture
- There's any ambiguity about what was established before

Say "Let me check memory..." before querying. Then:
- If memory has relevant facts → cite them specifically and ground your answer in them
- If memory is empty or returns nothing relevant → say "Memory doesn't have anything recorded about this" and ask if they'd like to share context you can store

**Query first, answer second.** The reason: a confident answer that contradicts a stored decision is far more damaging than taking a moment to check.

## When to Retract (minigraf_retract)

Retract when:
- The user explicitly says "remove", "delete", "retract", "forget", "that's no longer true"
- A fact has been superseded by a newer decision
- A fact was stored incorrectly

After retraction, say: "I've removed that from memory (the original is preserved in history)."

## What NOT to Store

Skip transient observations, intermediate reasoning, raw code snippets, and restatements of what the user just said. Store durable, cross-session facts only: decisions, preferences, constraints, dependencies, architecture.

## Naming Convention (Critical)

All attribute names follow a strict three-part path: `:namespace/entity-name/attribute-name`

**Valid namespaces:** `:project/`, `:preference/`, `:rules/`

Examples:
- `:project/postgres/name` — good
- `:project/postgres/role` — good
- `:preference/no-db-mocks/description` — good
- `:rules/python-version/description` — good
- `:rules/description` — **BAD** (missing entity name)
- `:project/name` — **BAD** (missing entity name)

Query memory first to find existing entity names before adding new facts about them.

## Tools

### minigraf_transact
```python
from minigraf_tool import transact

transact("""[[:project/postgres :project/postgres/name "PostgreSQL 15"]
             [:project/postgres :project/postgres/priority "ACID compliance + JSON support"]
             [:project/postgres :project/postgres/tradeoff "lower write throughput"]]""",
         reason="Database choice finalized — JSON support required for analytics queries")
```

Or via CLI (from project directory):
```bash
python minigraf_tool.py transact '[...]' --reason "why this is worth keeping"
```

### minigraf_query
```python
from minigraf_tool import query

# Broad scan
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")

# Targeted
result = query("[:find ?name :where [?e :project/postgres/name ?name]]")

# Temporal — what was stored before transaction 5
result = query("[:find ?v :as-of 5 :where [?e :project/postgres/priority ?v]]")
```

### minigraf_retract
```python
from minigraf_tool import retract
retract("[[:project/old-entity :project/old-entity/name \"obsolete\"]]",
        reason="Superseded by new decision")
```

## Quick Reference

### Aggregations
- `(count ?e)` / `(count-distinct ?e)` / `(sum ?n)` / `(min ?x)` / `(max ?x)`
- Group by: `[:find ?phase (count ?e) :where [?e :project/component/phase ?phase]]`

### Bi-temporal
- `:as-of N` — state at transaction N
- `:valid-at "2024-01-01"` — facts valid at date
- `:any-valid-time` — ignore valid-time filter

### Negation
- `(not [?e :attr val])` — exclude matches
- `(not-join [?e] [?e :attr ?x])` — existential negation

## Graph Storage

Default: `memory.graph` in the current working directory. Run all commands from the same project root to ensure consistent graph access.

## Dependencies

- **Minigraf >= 0.18.0** — `cargo install minigraf`
- **Python 3** — for the wrapper

## Examples

### Storing a tech stack decision
User: "We're using FastAPI over Flask — async support is critical for our Redis calls."
```python
transact("""[[:project/api-layer :project/api-layer/name "FastAPI"]
             [:project/api-layer :project/api-layer/rejected "Flask"]
             [:project/api-layer :project/api-layer/reason "async support required for Redis calls"]]""",
         reason="API framework finalized")
```

### Querying before modifying code
User: "Add connection pooling to the DB layer."
```python
# Before touching anything:
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Scan for :project/ facts — check if DB choice or constraints exist
```

### Handling empty memory
User: "What database did we decide on?"
```python
result = query("[:find ?name :where [?e :project/postgres/name ?name]]")
# result["results"] == []
```
Response: "Let me check memory... Memory doesn't have anything recorded about a database choice. If you share the decision, I'll store it for future sessions."

### Surfacing a constraint conflict
User: "Help me set up a MySQL connection."
```python
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Finds :project/postgres/name "PostgreSQL 15"
```
Response: "Before we proceed — memory shows we're using PostgreSQL 15 as the primary database. Is this a new secondary database, or has the decision changed? If it's changed, I'll update memory to reflect that."

### Storing a preference with context
User: "I hate mocks in DB tests — we got burned when mocked tests passed but the migration failed."
```python
transact("""[[:preference/no-db-mocks :preference/no-db-mocks/description "always use real database connections in tests"]
             [:preference/no-db-mocks :preference/no-db-mocks/reason "mock/prod divergence caused silent migration failure"]]""",
         reason="Strong team preference — backed by production incident")
```

## Error Responses

All functions return `{"ok": bool, ...}`. Common errors:
- `minigraf not found` — install via `cargo install minigraf`
- `No graph file at <path>` — call `transact()` first
- `as_of requires :as-of clause` — include `:as-of N` in query
- `reason is required for all writes` — provide non-empty reason

## Files

| File | Purpose |
|------|---------|
| `minigraf_tool.py` | Python wrapper (import or CLI) |
| `tools/query.json` | Tool schema for minigraf_query |
| `tools/transact.json` | Tool schema for minigraf_transact |
| `tools/retract.json` | Tool schema for minigraf_retract |
| `install.py` | Setup script |
| `ROADMAP.md` | Project roadmap |
