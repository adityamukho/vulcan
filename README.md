# Temporal Reasoning

**Perfect memory. Exact reasoning. Complete history.**

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.

## Questions Only Temporal Reasoning Can Answer

These queries are impossible with git log, vector search, or key-value memory:

```datalog
; What did the dependency graph look like before the auth refactor?
[:find ?caller ?callee
 :as-of 30
 :where [?caller :calls ?callee]]

; When did this coupling first appear — and what decision caused it?
[:find ?reason
 :where [:project/service-a :depends-on :project/service-b]
        [?d :motivated-by ?c]
        [?c :description ?reason]]

; Which modules were coupled to the payment service when we made the DB decision?
[:find ?module
 :as-of 15
 :where [?module :depends-on :service/payment]]
```

This is the only tool where both the decision and the structural change live as datoms in the same graph and can be joined in a single query. See [Phase 4](ROADMAP.md) for code structure evolution from git history.

## Why Temporal Reasoning?

Most memory tools for agents are key-value stores or vector databases. They answer "what do you know now?" Temporal Reasoning answers a harder question: **"what did you know then, and what changed?"**

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

**Graph traversal.** Entities are first-class nodes — not isolated key-value blobs. Store service-calls-service as a real graph edge (`:calls :project/auth-service`) and traverse it with Datalog joins. Fixed-depth transitive queries (2-hop, 3-hop) are expressed as multi-hop joins. Rules unify multiple edge types under a single named relation.

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
│         (vulcan.py - this repo)                         │
│   - query(), transact() functions                     │
│   - CLI mode                                           │
│   - Backup/restore utilities                           │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Minigraf CLI (>= 0.19.0)                   │
│         (https://github.com/adityamukho/minigraf)       │
│         (the storage engine)                       │
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

### Claude Code (plugin — recommended)

Add to your Claude Code `settings.json`:

```json
"extraKnownMarketplaces": {
  "temporal-reasoning": {
    "source": {
      "source": "git",
      "url": "https://github.com/adityamukho/temporal_reasoning"
    }
  }
}
```

Then enable the `temporal-reasoning` plugin in Claude Code. Once enabled, run once to download the minigraf binary:

```bash
python install.py
```

`install.py` auto-detects your platform and downloads the correct pre-built binary (Linux x86_64/aarch64, macOS arm64/x86_64, Windows). Falls back to `cargo install minigraf` on unsupported platforms.

### Manual install

```bash
git clone https://github.com/adityamukho/temporal_reasoning
cd temporal_reasoning
python install.py
```

### OpenCode

```bash
python install.py
```

This syncs the skill into `.opencode/skills/temporal-reasoning`.

## Quick Start

```python
from vulcan import query, transact

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
| `vulcan.py` | Python CLI wrapper |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | Setup script |
| `pyproject.toml` | Python packaging |
| `tools/*.json` | Tool schemas |
| `prompts/*.txt` | Behavioral prompts |
| `tests/test_harness.py` | Validation tests |

## Tools

- **vulcan_query** — Query memory with Datalog
- **vulcan_transact** — Store facts (reason required)
- **vulcan_retract** — Retract facts (original stays in history)
- **vulcan_report_issue** — File GitHub issues

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# Temporal query (state at transaction N)
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Aggregation
query("[:find (count ?e) :where [?e :decision/description ?d]]")

# Single-hop graph traversal — what does api-gateway call?
query("[:find ?name :where [:project/api-gateway :calls ?svc] [?svc :name ?name]]")

# Two-hop join — transitive impact: what depends on key-store (directly or via one intermediate)?
query("""[:find ?svc
          :where [?mid :depends-on :project/key-store]
                 [?svc :depends-on ?mid]]""")

# Decision traceability — why did we choose asyncio?
query("[:find ?reason :where [:decision/asyncio-choice :motivated-by ?c] [?c :description ?reason]]")

# Typed entity query — list all stored components
query("[:find ?name :where [?e :entity-type :type/component] [?e :name ?name]]")
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

Seven evals measure how the skill changes behavior versus a no-skill baseline. Each eval is seeded with a specific memory state and tests a distinct capability.

| Eval | What it tests | With Skill | Without Skill |
|------|--------------|-----------|---------------|
| Decision storage | Persists architectural decisions with correct naming + reasons | 6/6 | 0/6 |
| Populated retrieval | Queries memory and cites stored facts by name | 5/5 | 0/5 |
| Cross-session preference | Discovers and applies a constraint never stated in the current conversation | 4/4 | 0/4 |
| Conflict detection | Surfaces architectural conflicts before silently overriding decisions | 4/4 | 0/4 |
| Entity reference storage | Stores relationships as traversable graph edges, not dead-end strings | 5/5 | 0/5 |
| Transitive impact analysis | Traverses a multi-hop dependency chain to find all affected services | 5/5 | 0/5 |
| Decision traceability | Follows a `:motivated-by` edge to surface the constraint behind a decision | 5/5 | 0/5 |
| **Total** | | **34/34 (100%)** | **0/34 (0%)** |

The cross-session preference eval is the most discriminating for memory recall: the prompt says "make sure it fits with how we do things" with no hint that a relevant constraint exists. The skill queries memory, finds a stored no-mocks preference, and writes a test using real database connections.

The transitive impact eval is the most discriminating for graph traversal: given "key-store is being replaced — what breaks?" the skill executes a 2-hop Datalog join and returns a full impact chain; without it, the agent correctly admits it cannot name the affected services.

See [`evals/benchmark.md`](evals/benchmark.md) for full results and per-eval breakdowns.

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — Write policy, report_issue, install, skill benchmarks ✓
- **Phase 3** — WASM bindings, MCP integration (future)
- **Phase 4** — Code structure evolution from git history (future)
