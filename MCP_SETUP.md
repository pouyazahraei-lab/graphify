# Graphify MCP server: setup

Graphify ships a local MCP server (`graphify.serve`) that lets MCP-compatible agents
(Codex, Claude Code, Cursor, …) query a Graphify knowledge graph over **STDIO**. It runs
on your machine, uses no network, no paid API and no external AI. Tools are listed in
[MCP_TOOLS.md](MCP_TOOLS.md).

## 1. Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[mcp]"        # from a clone of this repo
# or, from PyPI:  pip install "graphifyy[mcp]"
```

Requires Python 3.10+. The `mcp` extra installs the official MCP Python SDK.

## 2. Build the graph (once, and after source changes)

```bash
graphify update .              # AST-only, local, no LLM; writes graphify-out/graph.json
```

Output goes to `graphify-out/` (git-ignored). Source files are only read, never modified.

## 3. Start the server

```bash
python -m graphify.serve graphify-out/graph.json
# equivalent console script:
graphify-mcp graphify-out/graph.json
```

It speaks MCP on stdin/stdout and waits for a client; it prints nothing else on stdout.
You normally don't run it by hand; the client launches it (below).

## 4. Connect Codex

Add to `~/.codex/config.toml` (use absolute paths):

```toml
[mcp_servers.graphify]
command = "/ABS/PATH/TO/.venv/bin/python"
args = ["-m", "graphify.serve", "/ABS/PATH/TO/REPO/graphify-out/graph.json"]
```

or via the CLI:

```bash
codex mcp add graphify -- /ABS/PATH/TO/.venv/bin/python -m graphify.serve /ABS/PATH/TO/REPO/graphify-out/graph.json
```

Restart Codex, then ask it to use the `graphify` tools. *Codex was not available in the
environment where this was developed, so the Codex config is documented from Codex's
standard `mcp_servers` format and was not run against Codex itself. The server was
verified with the official MCP Python client; see "Verify" below.*

## 5. Connect any other MCP client

Any client that launches a STDIO server needs the same three things:

| Field | Value |
|---|---|
| command | `/ABS/PATH/TO/.venv/bin/python` |
| args | `-m graphify.serve /ABS/PATH/TO/REPO/graphify-out/graph.json` |
| transport | `stdio` |

JSON form (Claude Code `.mcp.json`, Cursor, and most others):

```json
{
  "mcpServers": {
    "graphify": {
      "command": "/ABS/PATH/TO/.venv/bin/python",
      "args": ["-m", "graphify.serve", "/ABS/PATH/TO/REPO/graphify-out/graph.json"]
    }
  }
}
```

Claude Code: `claude mcp add graphify -- /ABS/PATH/TO/.venv/bin/python -m graphify.serve /ABS/PATH/TO/REPO/graphify-out/graph.json`

## Verify

```bash
python - <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    p = StdioServerParameters(command=sys.executable,
                              args=["-m", "graphify.serve", "graphify-out/graph.json"])
    async with stdio_client(p) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print([t.name for t in (await s.list_tools()).tools])
        res = await s.call_tool("search_knowledge", {"query": "build graph", "limit": 3})
        print(res.content[0].text)
asyncio.run(main())
PY
```

Run the tests: `pip install pytest && pytest tests/test_mcp_knowledge.py`.

## Security notes

- The server only reads the one `graph.json` it was started with. The new knowledge tools
  take no file path and reject `project_path`.
- No tool runs shell commands or accepts command text. Environment variables are never returned.
- Nothing is rebuilt or deleted automatically. `rebuild_knowledge_graph` runs only when a
  client calls it with `confirm: true`, rebuilds in-process (AST only), and keeps
  Graphify's guard that refuses to replace the graph with a much smaller one.
- STDIO only by default. (`--transport http` exists in the base server; it binds
  127.0.0.1 and isn't needed for this setup.)

## Limitations

- The graph is a snapshot: after code changes call `rebuild_knowledge_graph` (or run
  `graphify update .`). `get_graph_statistics` reports the commit the graph was built from.
- Rebuild covers code (AST). Docs/papers/images need the `/graphify` assistant workflow, which
  may use an LLM; the MCP server never calls one.
- Search is lexical/structural scoring, not semantic embeddings.
