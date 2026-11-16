"""MCP server (stdio) exposing the memory store to an AI assistant.

Read-only by default. ``--allow-write`` adds one tool, ``memory_remember``,
which writes a Markdown note into a local inbox folder and ingests it - it
never changes or deletes existing sources.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from graphify.memory import query as Q
from graphify.memory.store import MemoryStore

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _tools(allow_write: bool) -> list[dict]:
    tools = [
        {"name": "memory_search",
         "description": "Keyword search over everything ingested (notes, documents, emails, AI conversations, commits). "
                        "Returns ranked snippets, each with its source file, exact locator (line range, page, message id) "
                        "and timestamp, plus the entities mentioned. Use memory_get_chunk to read a full passage.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": _STR, "limit": _INT,
             "kind": {"type": "string", "description": "markdown|pdf|docx|xlsx|text|email|mbox|chatgpt|git"},
             "entity": {"type": "string", "description": "only chunks mentioning this entity (name or id)"},
             "since": {"type": "string", "description": "ISO date lower bound"},
             "until": {"type": "string", "description": "ISO date upper bound"},
             "path_contains": _STR,
             "mode": {"type": "string", "enum": ["all", "any"]}}}},
        {"name": "memory_get_chunk", "description": "Full text and provenance of one chunk by chunk_id.",
         "inputSchema": {"type": "object", "required": ["chunk_id"], "properties": {"chunk_id": _INT}}},
        {"name": "memory_find_entities",
         "description": "Find people, projects, companies, locations, concepts, decisions, tasks... by name (exact, then fuzzy).",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _STR, "type": _STR, "limit": _INT}}},
        {"name": "memory_entity",
         "description": "Everything known about one entity: aliases, attributes, recent mentions with sources, and relationships.",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _STR, "type": _STR, "mention_limit": _INT}}},
        {"name": "memory_related",
         "description": "Relationships of an entity. 'relations' are stated in a source (with evidence); 'cooccurring' "
                        "are entities that merely appear in the same passages (statistical, INFERRED).",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _STR, "relation": _STR, "limit": _INT}}},
        {"name": "memory_timeline", "description": "Dated passages, newest first, optionally for one entity and a date range.",
         "inputSchema": {"type": "object", "properties": {"name": _STR, "since": _STR, "until": _STR, "limit": _INT}}},
        {"name": "memory_list_entities", "description": "List entities of a type, most-mentioned first (e.g. type=task, status=open).",
         "inputSchema": {"type": "object", "properties": {"type": _STR, "status": _STR, "limit": _INT}}},
        {"name": "memory_stats", "description": "Store size, sources, entity counts and the last ingestion run.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    if allow_write:
        tools.append({
            "name": "memory_remember",
            "description": "Save a new fact, decision or preference as a Markdown note in the local memory inbox and "
                           "ingest it. Use frontmatter-style fields to link it: people, project, tags. Never edits existing sources.",
            "inputSchema": {"type": "object", "required": ["text"], "properties": {
                "text": _STR, "title": _STR, "type": {"type": "string", "description": "e.g. decision, preference, note"},
                "people": {"type": "array", "items": _STR}, "project": _STR,
                "tags": {"type": "array", "items": _STR}}}})
    return tools


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")[:60] or "note"


def remember(st: MemoryStore, inbox: Path, args: dict) -> dict:
    from graphify.memory.ingest import Ingestor

    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    title = (args.get("title") or args["text"].split("\n", 1)[0])[:80]
    fm = {"title": title, "date": stamp.isoformat(timespec="seconds"), "source": "jarvis"}
    if args.get("type"):
        fm["type"] = args["type"]
    if args.get("people"):
        fm["people"] = list(args["people"])
    if args.get("project"):
        fm["project"] = args["project"]
    if args.get("tags"):
        fm["tags"] = list(args["tags"])
    import yaml

    body = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + args["text"].strip() + "\n"
    if (args.get("type") or "").lower() == "decision" and not re.search(r"(?im)^\s*decision\s*:", body):
        body += f"\nDecision: {title}\n"
    path = inbox / f"{stamp.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}.md"
    path.write_text(body, encoding="utf-8")
    counts = Ingestor(st).ingest([inbox])
    return {"saved": str(path), "ingest": counts}


def _dispatch(st: MemoryStore, name: str, a: dict, inbox: Path | None):
    if name == "memory_search":
        return Q.search(st, a["query"], limit=int(a.get("limit") or 10), kind=a.get("kind"),
                        entity=a.get("entity"), since=a.get("since"), until=a.get("until"),
                        path_contains=a.get("path_contains"), mode=a.get("mode") or "all")
    if name == "memory_get_chunk":
        return Q.get_chunk(st, int(a["chunk_id"])) or {"error": "no such chunk"}
    if name == "memory_find_entities":
        return Q.find_entities(st, a["name"], etype=a.get("type"), limit=int(a.get("limit") or 10))
    if name == "memory_entity":
        return Q.entity(st, a["name"], etype=a.get("type"), mention_limit=int(a.get("mention_limit") or 10)) \
            or {"error": f"no entity named {a['name']!r}; try memory_find_entities"}
    if name == "memory_related":
        return Q.related(st, a["name"], rel=a.get("relation"), limit=int(a.get("limit") or 50))
    if name == "memory_timeline":
        return Q.timeline(st, a.get("name"), since=a.get("since"), until=a.get("until"), limit=int(a.get("limit") or 50))
    if name == "memory_list_entities":
        return Q.list_entities(st, etype=a.get("type"), status=a.get("status"), limit=int(a.get("limit") or 100))
    if name == "memory_stats":
        return Q.stats(st)
    if name == "memory_remember" and inbox is not None:
        return remember(st, inbox, a)
    raise KeyError(name)


def handle(st: MemoryStore, name: str, arguments: dict, inbox: Path | None = None) -> str:
    """Run one tool call and return its JSON text (shared with tests)."""
    try:
        result = _dispatch(st, name, dict(arguments or {}), inbox)
    except KeyError as exc:
        result = {"error": f"unknown tool or missing argument: {exc}"}
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)


def serve(db_path: str | None = None, *, allow_write: bool = False, inbox: str | None = None) -> None:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        import mcp.types as types
    except ImportError as exc:
        raise SystemExit('mcp not installed. Run: pip install "graphifyy[mcp]"') from exc
    import asyncio

    st = MemoryStore(db_path)
    inbox_path = Path(inbox).expanduser() if inbox else st.path.parent / "inbox"
    tool_defs = _tools(allow_write)

    async def list_tools():
        return [types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"]) for t in tool_defs]

    async def call_tool(name: str, arguments: dict):
        if name == "memory_remember" and not allow_write:
            text = json.dumps({"error": "server started read-only; restart with --allow-write"})
        else:
            text = handle(st, name, arguments, inbox_path if allow_write else None)
        return [types.TextContent(type="text", text=text)]

    if hasattr(Server, "list_tools"):
        server = Server("graphify-memory")
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
    else:   # mcp 2.x constructor-callback API (mirrors graphify/serve.py)
        async def _on_list_tools(ctx, params):
            return types.ListToolsResult(tools=await list_tools())

        async def _on_call_tool(ctx, params):
            return types.CallToolResult(content=await call_tool(params.name, dict(params.arguments or {})))

        server = Server("graphify-memory", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    async def main():
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    asyncio.run(main())
