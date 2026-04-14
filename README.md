# Temporal Reasoning

Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Problem Scope

This skill solves a specific problem: **AI coding agents forget context between conversations**.

What it does:
- **Stores** architecture decisions, constraints, and preferences
- **Queries** past state with temporal awareness
- **Persists** memory across sessions

What it is NOT:
- A general-purpose database
- A replacement for version control
- A code search tool

## Why minigraf?

Most memory tools for agents are key-value stores or vector databases. They answer "what do you know now?" minigraf answers a harder question: **"what did you know then?"**

**Time travel.** Every write is stamped with a transaction number. You can query the graph as it existed at any past transaction:

```python
# Decision made in session 1, transaction 3
transact('[[:project/db :name "PostgreSQL"]]', reason="Initial choice")

# Changed in session 4, transaction 11
retract('[[:project/db :name "PostgreSQL"]]', reason="Switching to CockroachDB for geo-distribution")
transact('[[:project/db :name "CockroachDB"]]', reason="Switching to CockroachDB for geo-distribution")

# Later: what did we think the database was before session 4?
query("[:find ?name :as-of 10 :where [:project/db :name ?name]]")
# → "PostgreSQL"

# What do we think now?
query("[:find ?name :where [:project/db :name ?name]]")
# → "CockroachDB"
```

**Retraction with preserved history.** Changing your mind doesn't erase the record. Retracted facts stay in the bi-temporal log and remain queryable at their original transaction time. This means the agent can always reconstruct *why* a decision changed, not just *what* the current state is.

**Exact Datalog queries, not fuzzy search.** Results are deterministic and reproducible — no embedding model, no similarity threshold, no hallucinated retrievals. A query either matches or it doesn't.

**Local and offline.** A single binary and a file. No API key, no network dependency, no cloud service to go down.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   AI Coding Agent                        │
│              (Claude Code, OpenCode, Codex)            │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Python Skill Layer                          │
│         (minigraf_tool.py - this repo)                  │
│   - query(), transact() functions                     │
│   - CLI and HTTP modes                                 │
│   - Backup/restore utilities                           │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Minigraf CLI (>= 0.18.0)                   │
│         (https://github.com/adityamukho/minigraf)       │
│   - Bi-temporal Datalog database                      │
│   - Transaction time + Valid time                      │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Graph File                                  │
│     memory.graph (current working directory)                        │
└─────────────────────────────────────────────────────────┘
```

## Install

```bash
# Install minigraf (requires Rust)
cargo install minigraf

# Run setup
python install.py
```

### Install In Agent Environments

Claude Code / Codex:
- Install the local skill from this repository as `temporal-reasoning`.
- Use [SKILL.md](/SKILL.md) and [skill.json](/skill.json) as the primary skill files.

OpenCode:
- Run `python install.py` from the repository root.
- This syncs the skill into `.opencode/skills/temporal-reasoning`.

If manual installation is required, include:
- [SKILL.md](/SKILL.md)
- [skill.json](/skill.json)
- [tools/query.json](/tools/query.json)
- [tools/transact.json](/tools/transact.json)
- [tools/report_issue.json](/tools/report_issue.json)

## Quick Start

```python
from minigraf_tool import query, transact

# Store a decision
transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", 
         reason="Architecture decision for low-latency caching")

# Query decisions
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Storage Location

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## Files

| File | Purpose |
|------|---------|
| `minigraf_tool.py` | Python CLI wrapper |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | Setup script |
| `pyproject.toml` | Python packaging |
| `tools/*.json` | Tool schemas |
| `prompts/*.txt` | Behavioral prompts |
| `tests/test_harness.py` | Validation tests |

## Tools

- **minigraf_query** — Query memory with Datalog
- **minigraf_transact** — Store facts (reason required)
- **minigraf_retract** — Retract facts (original stays in history)
- **minigraf_report_issue** — File GitHub issues

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# Temporal query (state at transaction N)
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Aggregation
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```

## Cross-Session Evaluation

The repository includes a deterministic evaluation showing that persisted memory
changes behavior in a later session without restating the original context.

Run:

```bash
pytest tests/test_harness.py -q
```

Success means the harness demonstrates all of the following against the same
graph file:
- A decision is stored in an earlier session.
- A later session answers a cache-strategy question using that persisted
  decision.
- A later session derives an action-oriented plan from the same persisted
  decision.

This evaluation is intentionally local and deterministic. It does not depend on
live model output, so it is suitable as repeatable evidence for the skill's
cross-session usefulness claim.

## Usefulness Benchmarks

The harness also reports two explicit benchmark-style metrics so usefulness
claims are tied to measurable output rather than broad narrative assertions.

- Behavior consistency:
  verifies that persisted memory drives both a later answer and a later
  action-oriented plan toward the same stored decision.
- Prompt compression proxy:
  compares a short prompt that relies on memory recall with a longer prompt
  that repeats the same decision context inline.

Run:

```bash
python tests/test_harness.py
```

The prompt-compression metric uses a simple whitespace word count as a stable
local proxy for prompt size. It does not claim model-token exactness; it only
shows that recalling stored context can reduce repeated prompt text in a later
session.

## Skill Benchmarks

Four evals measure how the skill changes behavior versus a no-skill baseline. Each eval is seeded with a specific memory state and tests a distinct capability.

| Eval | What it tests | With Skill | Without Skill |
|------|--------------|-----------|---------------|
| Decision storage | Persists architectural decisions with correct naming + reasons | 6/6 | 0/6 |
| Populated retrieval | Queries memory and cites stored facts by name | 5/5 | 0/5 |
| Cross-session preference | Discovers and applies a constraint never stated in the current conversation | 4/4 | 0/4 |
| Conflict detection | Surfaces architectural conflicts before silently overriding decisions | 4/4 | 0/4 |
| **Total** | | **19/19 (100%)** | **0/19 (0%)** |

The cross-session preference eval is the most discriminating: the prompt says "make sure it fits with how we do things" with no hint that a relevant constraint exists. The skill queries memory, finds a stored no-mocks preference from a prior session, and writes a test using real database connections — without being told to.

See [`evals/benchmark.md`](evals/benchmark.md) for full results and per-eval breakdowns.

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — Write policy, report_issue, install, skill benchmarks ✓
- **Phase 3** — WASM bindings, MCP integration (future)

## Marketplace Publishing

The skill is functionally complete and benchmarked. The blocking dependency for marketplace publication is **minigraf pre-built binaries**: `cargo install minigraf` requires a Rust toolchain, which is too high a barrier for general users. Publish once minigraf ships binaries for common platforms (Linux x86_64, macOS arm64/x86_64, Windows). At that point, also reframe the skill description to lead with the user benefit (no lost context between sessions) rather than the mechanism.
