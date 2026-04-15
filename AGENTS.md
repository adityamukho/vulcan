# Temporal Reasoning Repository

Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Architecture

```
[ Agent (Claude Code / OpenCode / Codex) ]
        ↓
[ Python Skill Layer ]              ← this repo
        ↓
[ Minigraf CLI ]                   ← must be on PATH (>= 0.19.0)
        ↓
[ .graph file on disk ]
```

## Dependencies

- **Minigraf >= 0.19.0** — run `python install.py` (downloads pre-built binary automatically)
- **Python 3** — for the CLI wrapper

## Files

| File | Purpose |
|------|---------|
| `vulcan.py` | Python CLI wrapper (import or run as CLI) |
| `tools/query.json` | Tool schema for `vulcan_query` |
| `tools/transact.json` | Tool schema for `vulcan_transact` |
| `skill.json` | Portable skill manifest |

## Usage

### As Python module:
```python
from vulcan import query, transact

transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]",
         reason="Architecture decision for low-latency caching")
result = query("[:find ?desc :where [?e :decision/description ?desc]]")
```

### As CLI:
```bash
python vulcan.py transact "[[:test :person/name \"Alice\"]]"
python vulcan.py query "[:find ?name :where [:test :person/name ?name]]"
```

### With minigraf directly (REPL):
```bash
echo "(transact [[:alice :person/name \"Alice\"]])" | minigraf --file memory.graph
echo "(query [:find ?name :where [:alice :person/name ?name]])" | minigraf --file memory.graph
```

## Key Conventions

- **QUERY before answering**: Always query memory before answering questions about past decisions, architecture, dependencies
- **TRANSACT with reason**: Every write should include a reason explaining why it's worth keeping
- **Only store durable facts**: decisions, architecture, dependencies, constraints, user preferences — NOT transient observations
- **Use namespaces**: `:component/`, `:module/`, `:file/`, `:decision/`, `:arch/`, `:user/`, `:task/`, `:fact/`