# Jarvis memory: `graphify memory`

A local, persistent knowledge memory built into this fork of graphify. It ingests
notes, documents, email, AI conversations and git history into one SQLite file, keeps
an exact source reference for every passage, extracts people, projects, decisions,
tasks and other entities using explicit rules (no LLM), updates incrementally, and
serves the result to an AI assistant over MCP or a JSON CLI.

Nothing leaves your computer. There are no API keys, no paid services and no network
calls in this layer.

---

## 1. How it fits into graphify

| Existing graphify (unchanged) | New `graphify/memory/` package |
|---|---|
| Code → knowledge graph via tree-sitter (offline) | Personal/professional sources → SQLite store |
| Docs/papers/images → semantic graph via an LLM run from your AI assistant | Deterministic, offline entity and relation rules |
| `graph.json`, `graph.html`, Obsidian export, MCP `serve` | `graphify memory export` writes a normal `graph.json`, so all of those work on the memory graph too |
| `detect.py` sensitive-file filter, PDF/DOCX/XLSX converters | Reused as-is |

Modules: `store.py` (schema, entity identity, transactions), `sources.py` (format
adapters), `ingest.py` (incremental scan, deletions, run log), `query.py` (retrieval),
`graph_export.py`, `mcp_server.py`, `cli.py`. The only edits to existing files are the
`memory` command dispatch (`cli.py`, `__main__.py`) and the package list in
`pyproject.toml`.

## 2. What is supported (tested)

| Source | Files | Chunking / locator | Entities taken from it |
|---|---|---|---|
| Markdown / Obsidian | `.md .markdown .mdx` | by heading, `L12-L40` line ranges | frontmatter `type:` (the note *is* that person/project/…), `aliases`, `people/attendees/author`, `project`, `company/client/employer`, `location`, `tags`, `topics`; `[[wikilinks]]`; `#tags`; `- [ ]` / `- [x]` tasks; lines starting `Decision:` and list items under a heading containing "Decision" |
| Plain text | `.txt .rst .log` | paragraphs, line ranges | name matches only |
| PDF | `.pdf` (needs `[pdf]` extra) | per page, `p.3` | name matches only |
| Word | `.docx` (needs `[office]`) | by heading of the converted text | as Markdown (tasks, `Decision:` lines) |
| Excel | `.xlsx` (needs `[office]`) | per sheet, `sheet:Budget` | name matches only |
| Email | `.eml`, `.mbox` (Gmail Takeout) | per message, `msg:2<message-id>` | From/To/Cc people, **keyed by email address** |
| ChatGPT export | the export `.zip`, or `conversations.json` | per message on the conversation's current branch, `conv:<id>/msg:<id>` | the conversation; name matches |
| Git repositories | any folder containing `.git` | per commit, `commit:<sha>` (last 1000) | the repository; commit authors as contributors |

Files that look like secrets (`.env`, `credentials.*`, keys, …) are skipped using
graphify's existing detector. Folders `.git`, `.obsidian`, `.trash`, `node_modules`,
virtualenvs and similar are skipped. Add a `.memoryignore` file (glob per line) to the
root of any source folder to exclude more.

### Confidence

- `EXTRACTED`: stated in the source (a frontmatter field, an email header, a
  checkbox, a wikilink, a commit author).
- `INFERRED`: a known entity's name or alias appears verbatim in the text (whole
  word, case-insensitive, at least 4 characters). A name shared by two entities is
  ambiguous and is **not** matched.
- `related` also returns `cooccurring` entities (seen in the same passages). That is
  reported separately because it is a statistical signal, not a stated relationship.

### Deduplication

- Same type + same normalised name (case, Unicode, spacing and edge punctuation
  ignored) → one entity. "Densen Engineering Ltd" and "Densen Engineering Ltd." merge;
  "…Limited" does not.
- People from email are keyed by address. A person note and an email sender merge when
  the display name is a full name (two or more words). Single-word names such as "Alex"
  never merge people automatically.
- `graphify memory dedup` lists likely duplicates for review; `graphify memory merge
  KEEP DROP` merges them, and the merge survives re-ingestion. Nothing is fuzzy-merged
  automatically.

### Incremental updates and recovery

- Each run re-scans all registered folders. A file is re-parsed only if its size or
  modification time changed **and** its SHA-256 changed.
- Each file is replaced inside one SQLite transaction. A crash or power cut leaves every
  file either fully old or fully new. The next run marks the dead run `interrupted` and
  carries on.
- Files that disappear are removed with everything derived from them. Entities left
  with no supporting source are removed too. If a whole registered folder is unreachable
  (unplugged drive, offline OneDrive), **nothing** is deleted and a warning is logged.
- Every action (added, updated, deleted, skip with reason, parse error) is written to
  the run log: `graphify memory log`.

---

## 3. Install on Windows 11

Prerequisites: Python 3.10–3.14 from python.org or `winget install Python.Python.3.12`,
and Git for Windows (`winget install Git.Git`).

```powershell
cd $HOME
git clone -b jarvis-memory https://github.com/pouyazahraei-lab/graphify.git
cd graphify
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".[mcp,pdf,office]"
graphify memory --help
```

The store defaults to `%USERPROFILE%\.graphify\memory\memory.db`. To keep it somewhere
else, set it once:

```powershell
setx GRAPHIFY_MEMORY_DB "D:\Jarvis\memory.db"    # new terminals pick it up
```

## 4. Import knowledge

```powershell
graphify memory ingest "C:\Users\Pouya\Documents\ObsidianVault"
graphify memory ingest "C:\Users\Pouya\Documents\Projects" "C:\Users\Pouya\Downloads\chatgpt-export.zip"
graphify memory ingest "C:\Users\Pouya\Mail\Takeout\Mail\All mail Including Spam and Trash.mbox"
graphify memory ingest            # re-scan everything registered; only changed files are processed
graphify memory sources --status skipped     # what was skipped, and why
graphify memory log                          # last runs and the latest run's log
```

Where to get the inputs:

- **ChatGPT:** Settings → Data controls → Export data. Ingest the zip that arrives by
  email as-is.
- **Gmail:** Google Takeout → Mail → `.mbox`.
- **Outlook:** save messages as `.eml`, or export via a mail client that writes
  `.mbox`. `.msg` and `.pst` are not supported (see limitations).
- **GitHub repositories:** clone them locally (`graphify clone <url>` works) and ingest
  the folder. READMEs and docs become passages; commits become dated history. For the
  *code* graph use graphify's own `graphify update <repo>`.

Make it automatic with Task Scheduler (runs locally, no cloud):

```powershell
schtasks /Create /SC HOURLY /TN "Jarvis memory ingest" /TR "\"%USERPROFILE%\graphify\.venv\Scripts\graphify.exe\" memory ingest --quiet"
```

### Writing notes so the graph is rich

Entity notes in Obsidian work best with a `type:` in frontmatter:

```markdown
---
type: project
status: active
people: ["[[Barbara Chen]]"]
company: Densen Engineering
location: Burnaby
tags: [envelope, hvac]
date: 2026-10-02
---
# Townhouse Heat Pump Study

## Decisions
- Penetrations go through the rim joist, not the cladding plane.

Decision: Use pre-manufactured flashing boots at every line-set penetration.

## Tasks
- [ ] Confirm electrical panel capacity with Barbara
```

## 5. Query knowledge

Output is JSON (for agents). Add `--text` to `search` for a readable view.

```powershell
graphify memory search "flashing boot" --text
graphify memory search "panel capacity" --kind email --since 2026-11-01
graphify memory search "heat pump" --entity "Barbara Chen"
graphify memory chunk 42                        # full passage
graphify memory entity "Townhouse Heat Pump Study"
graphify memory related "Barbara Chen"
graphify memory find "Barbra"                   # fuzzy name lookup
graphify memory entities --type task --status open
graphify memory entities --type decision
graphify memory timeline "Townhouse Heat Pump Study" --since 2026-10-01
graphify memory stats
```

Every hit carries its provenance:

```json
{
  "chunk_id": 6,
  "title": "Panel capacity for the Townhouse Heat Pump Study",
  "snippet": "… The existing 100A [panel] cannot take two heat pumps …",
  "source": {
    "file": "C:/Users/Pouya/Mail/densen.eml",
    "locator": "message",
    "kind": "email",
    "timestamp": "2026-11-10T09:15:00-08:00"
  },
  "entities": [
    {"id": "person:email:barbara@densen.example", "type": "person", "name": "Barbara Chen", "confidence": "EXTRACTED"},
    {"id": "project:townhouse heat pump study", "type": "project", "name": "Townhouse Heat Pump Study", "confidence": "INFERRED"}
  ]
}
```

Visual graph and the existing graphify tools:

```powershell
graphify memory export --out memory-graph        # graph.json + graph.html
start memory-graph\graph.html
graphify explain "Barbara Chen" --graph memory-graph\graph.json
graphify query "heat pump flashing" --graph memory-graph\graph.json
```

## 6. Connect Jarvis or another assistant (MCP)

`graphify memory serve` is an MCP server over stdio. Tools:

| Tool | Purpose |
|---|---|
| `memory_search` | ranked passages with source, locator, timestamp, entities; filters: kind, entity, since, until, path_contains |
| `memory_get_chunk` | full text of one passage |
| `memory_find_entities` | exact, then fuzzy, entity lookup |
| `memory_entity` | aliases, attributes, mentions, relationships |
| `memory_related` | stated relationships with evidence, plus co-occurrence |
| `memory_timeline` | dated passages, newest first |
| `memory_list_entities` | e.g. open tasks, all decisions |
| `memory_stats` | store summary and last run |
| `memory_remember` | **only with `--allow-write`**: saves a new Markdown note to a local inbox folder and ingests it. It never edits or deletes existing sources. |

Claude Desktop: edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jarvis-memory": {
      "command": "C:\\Users\\Pouya\\graphify\\.venv\\Scripts\\graphify.exe",
      "args": ["memory", "serve"]
    }
  }
}
```

Claude Code:

```powershell
claude mcp add jarvis-memory -- "$HOME\graphify\.venv\Scripts\graphify.exe" memory serve
```

Add `"--allow-write"` to the arguments if Jarvis should be able to save memories. Any
MCP client that can launch a local command works the same way. Clients that cannot
use MCP can call the CLI and parse the JSON. A REST API was not added: MCP plus the JSON
CLI covers local agents without opening a network port. Graphify's existing HTTP MCP
transport (`graphify serve`, API-key protected) can serve the exported `graph.json` if
one is needed later.

---

## 7. Test results

`tests/test_memory.py`: 28 tests covering every adapter (Markdown, text, PDF, DOCX,
XLSX, EML, MBOX, ChatGPT zip and JSON, git), line/page/message locators, sensitive-file
skipping, unchanged/touched/modified/deleted files, unreachable roots,
`.memoryignore`, transaction rollback on a crash mid-write, interrupted-run marking,
parse errors, wikilink resolution and re-targeting, INFERRED name matching and
ambiguity, email identity, dedup and persistent merges, search filters, fuzzy lookup,
timeline, graph export, the MCP tool handler, the CLI and the `graphify memory`
entry point. All 28 pass.

The full existing suite was run before and after the change in the same Linux
environment. 84 tests failed before the change and the same 84 fail after it. Those
failures come from the environment (no DNS for URL tests; optional tree-sitter grammars
such as HCL not installed), not from this branch. One extra failure appeared from this
work (an INFERRED score of 0.7 that is off graphify's confidence rubric) and was fixed.

This branch was developed and tested on Linux. The Windows-specific choices are:
forward-slash path keys, a stdlib-only store, UTF-8 output when piped, and no POSIX-only
calls. **It has not yet been run on Windows 11 itself.**

## 8. Limitations and next steps

- **No semantic or embedding search yet.** Search is keyword (SQLite FTS5 with English
  stemming) plus fuzzy entity names. Local embeddings, e.g. via Ollama, would be the
  next addition; graphify already has an Ollama backend.
- **No LLM entity extraction.** Free-text people, companies or decisions are found only
  when they are already known entities, or are stated explicitly (frontmatter, headers,
  `Decision:` lines). This avoids made-up relationships but misses unstated ones. An
  opt-in local-LLM pass (Ollama) is the natural next step, and its results should stay
  labelled `INFERRED`.
- No OCR: scanned PDFs are skipped with a logged reason.
- Not supported: Outlook `.msg`/`.pst`, Claude/Gemini conversation exports, `.doc`/
  `.xls`, `.pptx`, images, audio. ChatGPT exports keep only the conversation's current
  branch (regenerated alternatives are dropped on purpose).
- Word locators point to the heading section, not page numbers. Excel locators point to
  the sheet, not cells.
- Code files inside repositories are not ingested as text. Use graphify's code graph
  (`graphify update`) for code, and `graphify merge-graphs` to combine graphs.
- `graphify memory export` writes a simple graph, so when two entities have several
  relations only one edge is kept. The full detail stays in the store and the
  `related` query.
- Wikilink resolution and name matching are recomputed across the whole store when the
  set of known names changes. That is fast for a personal store (about 1 second for
  376 files / 4,400 passages in testing) but will slow down well beyond that scale.
- The memory store is a separate file from graphify's `graphify-out/` code graphs and
  needs its own backup. It is one file, so copying `memory.db` while no ingest is
  running is enough.
- Files on OneDrive "Files On-Demand" are downloaded when hashed. Consider excluding
  very large online-only folders with `.memoryignore`.
