"""Knowledge-oriented MCP tool surface for the Graphify server.

Adds six agent-friendly tools on top of the existing ``graphify.serve`` MCP
server (search_knowledge, query_knowledge, get_entity, get_related_entities,
get_graph_statistics, rebuild_knowledge_graph). Four of them are thin, validated
aliases over the existing graph tools; ``search_knowledge`` and
``rebuild_knowledge_graph`` are implemented here.

Security posture:
- None of these tools accepts a filesystem path or a command. The graph that is
  read (and rebuilt) is the one the server was configured with; ``project_path``
  is rejected so a client cannot point the server at another directory.
- ``rebuild_knowledge_graph`` only runs when called with ``confirm=true``, runs
  the in-process AST rebuild (no subprocess, no LLM, no network), never edits
  source files, and leaves the node-count shrink guard on (no ``force``).
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from graphify.security import sanitize_label

NEW_TOOL_NAMES = (
    "search_knowledge",
    "query_knowledge",
    "get_entity",
    "get_related_entities",
    "get_graph_statistics",
    "rebuild_knowledge_graph",
)

MAX_QUERY_CHARS = 500
MAX_RESULTS = 50


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}


def tool_specs() -> list[dict]:
    """Name/description/inputSchema for each tool (plain dicts; serve.py wraps them)."""
    return [
        {
            "name": "search_knowledge",
            "description": (
                "Search the knowledge graph for concepts, files, entities and keywords. "
                "Returns the best-matching nodes (label, id, source file, type, community)."
            ),
            "inputSchema": _object({
                "query": {"type": "string", "description": "Keywords, symbol name, or file name."},
                "limit": {"type": "integer", "description": f"Max results (1-{MAX_RESULTS}, default 10)."},
                "file_type": {"type": "string",
                              "description": "Optional filter on node type, e.g. code, document, paper."},
            }, ["query"]),
        },
        {
            "name": "query_knowledge",
            "description": (
                "Answer a question using graph relationships: finds the relevant nodes and "
                "traverses their connections, returning nodes and edges within a token budget."
            ),
            "inputSchema": _object({
                "question": {"type": "string", "description": "Natural-language question or keywords."},
                "mode": {"type": "string", "enum": ["bfs", "dfs"], "description": "Traversal mode (default bfs)."},
                "depth": {"type": "integer", "description": "Traversal depth (default 3, max 6)."},
                "token_budget": {"type": "integer", "description": "Approximate output cap (default 2000)."},
            }, ["question"]),
        },
        {
            "name": "get_entity",
            "description": "Get details for one entity (node): source location, type, community, degree.",
            "inputSchema": _object({
                "entity": {"type": "string", "description": "Entity label or node id."},
            }, ["entity"]),
        },
        {
            "name": "get_related_entities",
            "description": "List the entities directly connected to an entity, with relation and confidence.",
            "inputSchema": _object({
                "entity": {"type": "string", "description": "Entity label or node id."},
                "relation_filter": {"type": "string", "description": "Optional relation name, e.g. calls, imports."},
                "token_budget": {"type": "integer", "description": "Approximate output cap (default 2000)."},
            }, ["entity"]),
        },
        {
            "name": "get_graph_statistics",
            "description": "Node, edge and community counts, confidence breakdown and build metadata.",
            "inputSchema": _object({}),
        },
        {
            "name": "rebuild_knowledge_graph",
            "description": (
                "Rebuild the configured code graph from its source files (AST only, local, no LLM). "
                "Only runs when confirm=true. Never modifies source files; refuses to replace the "
                "graph with a smaller one (safety guard)."
            ),
            "inputSchema": _object({
                "confirm": {"type": "boolean",
                            "description": "Must be true. Rebuild is never run implicitly."},
            }, ["confirm"]),
        },
    ]


# ---- argument validation -------------------------------------------------

def _clean_text(arguments: dict, key: str, err) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise err(f"'{key}' is required and must be a non-empty string.")
    if len(value) > MAX_QUERY_CHARS:
        raise err(f"'{key}' is too long (max {MAX_QUERY_CHARS} characters).")
    if "\x00" in value:
        raise err(f"'{key}' contains an invalid character.")
    return value.strip()


def _clamp_int(arguments: dict, key: str, default: int, lo: int, hi: int, err) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise err(f"'{key}' must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise err(f"'{key}' must be an integer.") from None
    return max(lo, min(hi, number))


def normalize_entity_args(arguments: dict, err) -> dict:
    """Map the public ``entity`` argument onto the existing node tools' ``label``."""
    out = {k: v for k, v in arguments.items() if k != "entity"}
    out["label"] = _clean_text(arguments, "entity", err)
    if "token_budget" in out:
        out["token_budget"] = _clamp_int(out, "token_budget", 2000, 100, 20000, err)
    if "relation_filter" in out and not isinstance(out["relation_filter"], str):
        raise err("'relation_filter' must be a string.")
    return out


def normalize_query_args(arguments: dict, err) -> dict:
    out = dict(arguments)
    out["question"] = _clean_text(arguments, "question", err)
    if out.get("mode", "bfs") not in ("bfs", "dfs"):
        raise err("'mode' must be 'bfs' or 'dfs'.")
    out["depth"] = _clamp_int(out, "depth", 3, 1, 6, err)
    out["token_budget"] = _clamp_int(out, "token_budget", 2000, 100, 20000, err)
    # Only these keys are forwarded; anything else (e.g. context_filter) is dropped.
    return {k: out[k] for k in ("question", "mode", "depth", "token_budget") if k in out}


# ---- search --------------------------------------------------------------

def search_knowledge(G, arguments: dict, err) -> str:
    from graphify.serve import _query_terms, _score_nodes

    query = _clean_text(arguments, "query", err)
    limit = _clamp_int(arguments, "limit", 10, 1, MAX_RESULTS, err)
    file_type = arguments.get("file_type")
    if file_type is not None and not isinstance(file_type, str):
        raise err("'file_type' must be a string.")
    terms = _query_terms(query) or query.lower().split()
    ranked = _score_nodes(G, terms)
    lines = []
    for score, nid in ranked:
        d = G.nodes[nid]
        if file_type and str(d.get("file_type", "")).lower() != file_type.lower():
            continue
        lines.append(
            f"- {sanitize_label(d.get('label', nid))}\n"
            f"    id: {sanitize_label(nid)}\n"
            f"    source: {sanitize_label(str(d.get('source_file', '')))} "
            f"{sanitize_label(str(d.get('source_location', '')))}\n"
            f"    type: {sanitize_label(str(d.get('file_type', '')))}  "
            f"community: {sanitize_label(str(d.get('community_name') or d.get('community', '')))}  "
            f"score: {score:.2f}"
        )
        if len(lines) >= limit:
            break
    if not lines:
        return f"No matches for {query!r}."
    return f"Top {len(lines)} matches for {query!r}:\n" + "\n".join(lines)


# ---- rebuild -------------------------------------------------------------

def resolve_rebuild_root(graph_path: str) -> Path:
    """Source root for the configured graph: the saved scan root, else the
    directory that contains the graph's output dir. Never taken from a request."""
    out_dir = Path(graph_path).resolve().parent
    marker = out_dir / ".graphify_root"
    if marker.is_file():
        try:
            saved = Path(marker.read_text(encoding="utf-8-sig").strip())
            if saved.is_dir():
                return saved.resolve()
        except OSError:
            pass
    return out_dir.parent


def rebuild_knowledge_graph(graph_path: str, arguments: dict, err) -> str:
    if arguments.get("confirm") is not True:
        raise err("Rebuild not run: pass confirm=true to explicitly rebuild the graph.")
    root = resolve_rebuild_root(graph_path)
    if not root.is_dir():
        raise err("Rebuild not run: configured source root does not exist.")
    from graphify.watch import _rebuild_code

    # The rebuild prints progress to stdout; on the STDIO transport stdout IS the
    # protocol channel, so route it to stderr for the duration.
    with contextlib.redirect_stdout(sys.stderr):
        ok = _rebuild_code(root, incremental=True, block_on_lock=False)
    if not ok:
        raise err(
            "Rebuild did not complete (another rebuild may be running, the safety guard "
            "refused a smaller graph, or extraction failed). The existing graph was kept."
        )
    return f"Rebuild complete for {root.name}. The graph reloads automatically on the next query."
