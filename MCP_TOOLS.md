# Graphify MCP tools

Server: `python -m graphify.serve <graph.json>` (STDIO). See [MCP_SETUP.md](MCP_SETUP.md).
All results are plain text. Invalid input returns an MCP error result (`isError: true`)
with a short message. The six tools below take **no file paths**; `project_path` is rejected.

| Tool | Arguments | Purpose |
|---|---|---|
| `search_knowledge` | `query` (str, required, ≤500 chars), `limit` (1-50, default 10), `file_type` (e.g. `code`, `document`) | Ranked matching nodes: label, id, source file/line, type, community, score. |
| `query_knowledge` | `question` (str, required), `mode` (`bfs`\|`dfs`), `depth` (1-6, default 3), `token_budget` (100-20000, default 2000) | Finds seed nodes for the question and traverses relationships; returns nodes and edges within the budget. |
| `get_entity` | `entity` (label or node id, required) | Source location, type, community, degree. Ambiguous names return candidates instead of guessing. |
| `get_related_entities` | `entity` (required), `relation_filter` (e.g. `calls`, `imports`), `token_budget` | Directly connected entities with direction, relation, confidence (`EXTRACTED`/`INFERRED`/`AMBIGUOUS`) and location. |
| `get_graph_statistics` | none | Node/edge/community counts, confidence breakdown, build commit and whether HEAD moved. |
| `rebuild_knowledge_graph` | `confirm` (must be boolean `true`) | Rebuilds the configured code graph (AST only, local). Never runs implicitly; never touches sources; refuses to shrink the graph below Graphify's safety threshold. |

The base server's earlier tools (`query_graph`, `get_node`, `get_neighbors`, `get_community`,
`god_nodes`, `graph_stats`, `shortest_path`, PR tools) are unchanged and still available.

## Example calls

```json
{"name": "search_knowledge", "arguments": {"query": "build graph", "limit": 3}}
{"name": "get_entity", "arguments": {"entity": "_build_server"}}
{"name": "get_related_entities", "arguments": {"entity": "_build_server", "relation_filter": "calls"}}
{"name": "query_knowledge", "arguments": {"question": "how does the MCP server load graph.json"}}
{"name": "rebuild_knowledge_graph", "arguments": {"confirm": true}}
```

## Errors

- Missing/empty/oversized/non-string `query`/`question`/`entity`, bad `mode`: error result.
- No match: normal result saying so (`No matches for ...`).
- Missing `graph.json`: the server still starts; graph tools report the file is not found
  until `graphify update .` (or `rebuild_knowledge_graph`) creates it.
- `rebuild_knowledge_graph` without `confirm: true`: error, nothing changes.
