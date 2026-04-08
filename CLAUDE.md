# Temporal Reasoning - AI Coding Agent Memory

This project provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill
python install.py

# Use in code
from minigraf_tool import query, transact

transact("[[:decision/cache :decision/description \"use Redis\"]]", reason="Caching strategy")
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Key Files

- `minigraf_tool.py` - Python wrapper for minigraf CLI
- `SKILL.md` - Skill definition with all query syntax
- `prompts/system.txt` - Behavioral prompts
- `install.py` - Setup script (runs weekly updates)

## Graph Storage

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# With temporal
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```
