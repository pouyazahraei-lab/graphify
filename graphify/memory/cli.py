"""``graphify memory ...`` command line. Output is JSON unless --text is given."""
from __future__ import annotations

import argparse
import json
import sys

from graphify.memory import query as Q
from graphify.memory.store import MemoryStore, default_db_path


def _print(obj, text: bool) -> None:
    if not text:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(obj, dict) and "results" in obj:
        if obj.get("note"):
            print(f"({obj['note']})")
        for i, r in enumerate(obj["results"], 1):
            s = r["source"]
            where = s["file"] + (f" [{s['unit']}]" if s.get("unit") else "") + f" @ {s['locator']}"
            print(f"{i}. {r['title'] or s['source_title']}  ({s['timestamp'] or 'undated'})\n   {where}\n   {r['snippet']}")
            if r["entities"]:
                print("   entities: " + ", ".join(f"{e['name']} ({e['type']})" for e in r["entities"][:8]))
        return
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graphify memory", description="Local, persistent knowledge memory.")
    p.add_argument("--db", default=None, help=f"SQLite store (default: $GRAPHIFY_MEMORY_DB or {default_db_path()})")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ingest", help="ingest folders/files; with no paths, re-scan every registered source")
    a.add_argument("paths", nargs="*")
    a.add_argument("--force", action="store_true", help="re-parse even unchanged files")
    a.add_argument("--no-git-history", action="store_true", help="do not read git commit history of repositories")
    a.add_argument("--no-register", action="store_true", help="ingest once without adding to the registered sources")
    a.add_argument("--quiet", action="store_true")

    r = sub.add_parser("forget-source", help="unregister a folder and delete everything derived from it")
    r.add_argument("path")

    s = sub.add_parser("search", help="keyword search with source references")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--kind")
    s.add_argument("--entity")
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--path-contains")
    s.add_argument("--any", action="store_true", help="match any term instead of all")
    s.add_argument("--text", action="store_true", help="human-readable output")

    c = sub.add_parser("chunk", help="full text of a chunk by id")
    c.add_argument("chunk_id", type=int)

    e = sub.add_parser("entity", help="an entity with aliases, mentions and relations")
    e.add_argument("name")
    e.add_argument("--type")
    e.add_argument("--mentions", type=int, default=10)

    f = sub.add_parser("find", help="find entities by name (exact then fuzzy)")
    f.add_argument("name")
    f.add_argument("--type")
    f.add_argument("--limit", type=int, default=10)

    rel = sub.add_parser("related", help="relationships of an entity")
    rel.add_argument("name")
    rel.add_argument("--relation")
    rel.add_argument("--limit", type=int, default=50)

    t = sub.add_parser("timeline", help="dated passages, newest first")
    t.add_argument("name", nargs="?")
    t.add_argument("--since")
    t.add_argument("--until")
    t.add_argument("--limit", type=int, default=50)

    le = sub.add_parser("entities", help="list entities")
    le.add_argument("--type")
    le.add_argument("--status")
    le.add_argument("--limit", type=int, default=100)

    so = sub.add_parser("sources", help="list ingested files and their status")
    so.add_argument("--status", choices=["active", "skipped", "error", "deleted"])
    so.add_argument("--limit", type=int, default=200)

    sub.add_parser("stats", help="store summary")

    lg = sub.add_parser("log", help="ingestion runs and the log of one run")
    lg.add_argument("--run", type=int)
    lg.add_argument("--limit", type=int, default=10)

    d = sub.add_parser("dedup", help="list likely duplicate entities for review (nothing is merged)")
    d.add_argument("--type")
    d.add_argument("--threshold", type=float, default=90.0)

    m = sub.add_parser("merge", help="merge entity DROP into KEEP (persists across re-ingestion)")
    m.add_argument("keep")
    m.add_argument("drop")

    x = sub.add_parser("export", help="write a graphify graph.json (+ graph.html) of the memory graph")
    x.add_argument("--out", default="memory-graph")
    x.add_argument("--no-html", action="store_true")
    x.add_argument("--no-tasks", action="store_true", help="leave tasks and decisions out")
    x.add_argument("--no-tags", action="store_true")

    sv = sub.add_parser("serve", help="run the MCP server over stdio")
    sv.add_argument("--allow-write", action="store_true", help="enable the memory_remember tool")
    sv.add_argument("--inbox", help="folder memory_remember writes notes into (default: next to the db)")
    return p


def _utf8_stdout() -> None:
    """Piped output on Windows defaults to the ANSI code page (cp1252), which
    cannot encode Persian or other non-Latin text; JSON consumers expect UTF-8."""
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "serve":
        _utf8_stdout()
    if args.cmd == "serve":
        from graphify.memory.mcp_server import serve

        serve(args.db, allow_write=args.allow_write, inbox=args.inbox)
        return 0
    st = MemoryStore(args.db)
    try:
        if args.cmd == "ingest":
            from graphify.memory.ingest import Ingestor

            res = Ingestor(st, git_history=not args.no_git_history, verbose=not args.quiet).ingest(
                args.paths or None, register=not args.no_register, force=args.force)
            _print(res, False)
            return 0 if res["status"] in ("ok", "ok_with_errors") else 1
        if args.cmd == "forget-source":
            from pathlib import Path
            import os

            key = Path(os.path.abspath(Path(args.path).expanduser())).as_posix()
            removed = 0
            with st.tx() as db:
                for row in list(db.execute("SELECT path FROM files WHERE root = ? AND status != 'deleted'", (key,))):
                    st.delete_file_sources(db, row["path"])
                    removed += 1
                db.execute("DELETE FROM files WHERE root = ?", (key,))
            existed = st.remove_root(Path(key))
            st.resolve_links()
            st.match_names(None)
            st.garbage_collect()
            _print({"root": key, "registered": existed, "files_removed": removed}, False)
            return 0
        if args.cmd == "search":
            _print(Q.search(st, args.query, limit=args.limit, kind=args.kind, entity=args.entity, since=args.since,
                            until=args.until, path_contains=args.path_contains, mode="any" if args.any else "all"),
                   args.text)
        elif args.cmd == "chunk":
            _print(Q.get_chunk(st, args.chunk_id) or {"error": "no such chunk"}, False)
        elif args.cmd == "entity":
            res = Q.entity(st, args.name, etype=args.type, mention_limit=args.mentions)
            _print(res or {"error": f"no entity named {args.name!r}", "suggestions": Q.find_entities(st, args.name, limit=5)}, False)
            return 0 if res else 2
        elif args.cmd == "find":
            _print(Q.find_entities(st, args.name, etype=args.type, limit=args.limit), False)
        elif args.cmd == "related":
            _print(Q.related(st, args.name, rel=args.relation, limit=args.limit), False)
        elif args.cmd == "timeline":
            _print(Q.timeline(st, args.name, since=args.since, until=args.until, limit=args.limit), False)
        elif args.cmd == "entities":
            _print(Q.list_entities(st, etype=args.type, status=args.status, limit=args.limit), False)
        elif args.cmd == "sources":
            _print(Q.list_sources(st, status=args.status, limit=args.limit), False)
        elif args.cmd == "stats":
            _print(Q.stats(st), False)
        elif args.cmd == "log":
            _print(Q.runs(st, limit=args.limit, run_id=args.run), False)
        elif args.cmd == "dedup":
            _print(Q.dedup_candidates(st, etype=args.type, threshold=args.threshold), False)
        elif args.cmd == "merge":
            _print(st.merge_entities(args.keep, args.drop), False)
        elif args.cmd == "export":
            from graphify.memory.graph_export import export

            _print(export(st, args.out, html=not args.no_html, include_tasks=not args.no_tasks,
                          include_tags=not args.no_tags), False)
        return 0
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
