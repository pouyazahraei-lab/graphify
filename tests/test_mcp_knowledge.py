"""End-to-end tests for the knowledge MCP tools (graphify.mcp_knowledge).

They launch the real server as a subprocess over STDIO and talk to it with the
official MCP client, so startup, discovery, tool calls, errors and rebuild are
all exercised the way Codex or any other client would use them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from graphify.mcp_knowledge import NEW_TOOL_NAMES  # noqa: E402

GRAPH = {
    "directed": True,
    "nodes": [
        {"id": "a", "label": "PaymentService", "file_type": "code", "source_file": "pay.py",
         "source_location": "L1", "community": 0},
        {"id": "b", "label": "InvoiceStore", "file_type": "code", "source_file": "inv.py",
         "source_location": "L3", "community": 0},
        {"id": "c", "label": "Refund policy", "file_type": "document", "source_file": "policy.md",
         "source_location": "L1", "community": 1},
    ],
    "links": [
        {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED",
         "source_file": "pay.py", "source_location": "L2"},
        {"source": "a", "target": "c", "relation": "references", "confidence": "INFERRED",
         "source_file": "pay.py", "source_location": "L9"},
    ],
}


def _err(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


def _run_session(graph_path: Path, body, cwd: Path | None = None):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "graphify.serve", str(graph_path)], cwd=str(cwd) if cwd else None
    )

    async def main():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await body(s)

    return asyncio.run(asyncio.wait_for(main(), 120))


def _text(res) -> str:
    return res.content[0].text


@pytest.fixture()
def graph_file(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    p = out / "graph.json"
    p.write_text(json.dumps(GRAPH), encoding="utf-8")
    return p


def test_server_starts_and_discovers_tools(graph_file):
    async def body(s):
        return [t.name for t in (await s.list_tools()).tools]

    names = _run_session(graph_file, body)
    for n in NEW_TOOL_NAMES:
        assert n in names
    assert "query_graph" in names  # existing tools preserved


def test_tool_schemas_have_no_path_parameters(graph_file):
    async def body(s):
        return {t.name: t for t in (await s.list_tools()).tools}

    tools = _run_session(graph_file, body)
    for n in NEW_TOOL_NAMES:
        schema = getattr(tools[n], "input_schema", None) or tools[n].inputSchema
        assert "project_path" not in schema.get("properties", {})


def test_search_entity_related_stats(graph_file):
    async def body(s):
        out = {}
        out["search"] = await s.call_tool("search_knowledge", {"query": "payment"})
        out["filter"] = await s.call_tool("search_knowledge", {"query": "refund policy", "file_type": "code"})
        out["entity"] = await s.call_tool("get_entity", {"entity": "PaymentService"})
        out["related"] = await s.call_tool("get_related_entities", {"entity": "PaymentService"})
        out["relf"] = await s.call_tool(
            "get_related_entities", {"entity": "PaymentService", "relation_filter": "calls"})
        out["stats"] = await s.call_tool("get_graph_statistics", {})
        out["query"] = await s.call_tool("query_knowledge", {"question": "PaymentService connections"})
        return out

    r = _run_session(graph_file, body)
    assert "PaymentService" in _text(r["search"]) and "pay.py" in _text(r["search"])
    assert "No matches" in _text(r["filter"]) or "PaymentService" not in _text(r["filter"])
    assert "pay.py" in _text(r["entity"]) and "Degree: 2" in _text(r["entity"])
    rel = _text(r["related"])
    assert "InvoiceStore" in rel and "Refund policy" in rel
    relf = _text(r["relf"])
    assert "InvoiceStore" in relf and "Refund policy" not in relf
    assert "Nodes: 3" in _text(r["stats"]) and "Edges: 2" in _text(r["stats"])
    assert "InvoiceStore" in _text(r["query"])
    assert not any(_err(v) for v in r.values())


def test_invalid_inputs_are_tool_errors(graph_file):
    async def body(s):
        calls = [
            ("search_knowledge", {}),
            ("search_knowledge", {"query": "   "}),
            ("search_knowledge", {"query": 123}),
            ("search_knowledge", {"query": "x" * 5000}),
            ("search_knowledge", {"query": "a\x00b"}),
            ("query_knowledge", {"question": ""}),
            ("query_knowledge", {"question": "ok", "mode": "sideways"}),
            ("get_entity", {}),
            ("get_related_entities", {"entity": ""}),
        ]
        return [(c, await s.call_tool(*c)) for c in calls]

    for call, res in _run_session(graph_file, body):
        assert _err(res), call
    # a well-formed query that matches nothing is a normal result, not a crash
    async def body2(s):
        return await s.call_tool("search_knowledge", {"query": "zzzzqqqq"}), \
            await s.call_tool("get_entity", {"entity": "NoSuchEntityAnywhere"})

    none, ent = _run_session(graph_file, body2)
    assert "No matches" in _text(none)
    assert "NoSuchEntityAnywhere" in _text(ent) or "No node" in _text(ent) or "not found" in _text(ent).lower()


def test_missing_graph_does_not_crash_server(tmp_path):
    missing = tmp_path / "graphify-out" / "graph.json"

    async def body(s):
        tools = await s.list_tools()
        res = await s.call_tool("search_knowledge", {"query": "anything"})
        return len(tools.tools), res

    n, res = _run_session(missing, body)
    assert n >= len(NEW_TOOL_NAMES)
    assert "not found" in _text(res).lower() or "no such" in _text(res).lower() or _err(res)


def test_path_security(graph_file, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")

    async def body(s):
        out = []
        for n in ("search_knowledge", "get_entity", "get_graph_statistics", "query_knowledge",
                  "get_related_entities"):
            args = {"query": "x"} if n == "search_knowledge" else {"question": "x"} if n == "query_knowledge" \
                else {} if n == "get_graph_statistics" else {"entity": "x"}
            out.append(await s.call_tool(n, {**args, "project_path": str(tmp_path)}))
        out.append(await s.call_tool("get_entity", {"entity": "../../secret.txt"}))
        out.append(await s.call_tool("search_knowledge", {"query": str(secret)}))
        out.append(await s.call_tool("rebuild_knowledge_graph", {"confirm": True, "project_path": "/tmp"}))
        return out

    res = _run_session(graph_file, body)
    for r in res[:5]:
        assert _err(r) and "project_path" in _text(r)
    assert all("TOPSECRET" not in _text(r) for r in res)
    assert _err(res[7]) and "project_path" in _text(res[7])
    assert secret.read_text(encoding="utf-8") == "TOPSECRET"


def test_no_env_or_secret_leak(graph_file, monkeypatch):
    monkeypatch.setenv("MY_SUPER_SECRET_TOKEN", "sk-leak-me-123")

    async def body(s):
        outs = [await s.call_tool("get_graph_statistics", {}),
                await s.call_tool("search_knowledge", {"query": "sk-leak-me-123 MY_SUPER_SECRET_TOKEN"})]
        return "".join(_text(o) for o in outs)

    assert "sk-leak-me-123" not in _run_session(graph_file, body).replace("sk-leak-me-123 MY_SUPER", "")


def test_rebuild_requires_explicit_confirm(graph_file):
    before = graph_file.read_bytes()

    async def body(s):
        return [await s.call_tool("rebuild_knowledge_graph", a)
                for a in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1})]

    for r in _run_session(graph_file, body):
        assert _err(r) and "confirm" in _text(r)
    assert graph_file.read_bytes() == before


def test_rebuild_updates_graph_and_leaves_sources_untouched(tmp_path):
    pytest.importorskip("tree_sitter")
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "app.py"
    src.write_text("def alpha_feature():\n    return 1\n", encoding="utf-8")
    from graphify.watch import _rebuild_code

    assert _rebuild_code(repo, block_on_lock=True)
    graph = repo / "graphify-out" / "graph.json"
    assert graph.exists()
    src.write_text(src.read_text() + "\n\ndef beta_feature():\n    return alpha_feature()\n", encoding="utf-8")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    async def body(s):
        before = await s.call_tool("search_knowledge", {"query": "beta_feature"})
        reb = await s.call_tool("rebuild_knowledge_graph", {"confirm": True})
        after = await s.call_tool("search_knowledge", {"query": "beta_feature"})
        return before, reb, after

    before, reb, after = _run_session(graph, body, cwd=repo)
    assert not _err(reb), _text(reb)
    assert "id: app_beta_feature" in _text(after)
    assert "id: app_beta_feature" not in _text(before)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest
