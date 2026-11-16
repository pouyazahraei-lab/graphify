"""Export the memory store as a regular graphify graph.

The result is a normal ``graph.json`` (+ optional ``graph.html``), so the
existing tools work on it: ``graphify query/explain/path --graph ...``, the
graphify MCP server (``python -m graphify.serve <graph.json>``), the HTML
viewer and the Obsidian exporter.
"""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from graphify.memory.store import MemoryStore

_FILE_TYPE = {"document": "document", "conversation": "document", "repository": "code"}


def build_graph(st: MemoryStore, *, include_tasks: bool = True, include_tags: bool = True,
                include_inferred_mentions: bool = True) -> nx.Graph:
    G = nx.Graph()
    first_source: dict[str, tuple[str, str]] = {}
    for r in st.db.execute(
            "SELECT m.entity_id, s.file_path, c.locator FROM mentions m JOIN chunks c ON c.id = m.chunk_id "
            "JOIN sources s ON s.id = c.source_id ORDER BY m.method = 'source' DESC, c.id"):
        first_source.setdefault(r["entity_id"], (r["file_path"].split("#", 1)[0], r["locator"]))
    skip = set()
    if not include_tasks:
        skip |= {"task", "decision"}
    if not include_tags:
        skip.add("tag")
    for e in st.db.execute("SELECT * FROM entities"):
        if e["type"] in skip:
            continue
        src, loc = first_source.get(e["id"], ("", ""))
        attrs = json.loads(e["attrs"] or "{}")
        G.add_node(e["id"], label=e["name"], file_type=_FILE_TYPE.get(e["type"], "concept"),
                   entity_type=e["type"], source_file=src, source_location=loc,
                   **{f"attr_{k}": v for k, v in attrs.items() if isinstance(v, (str, int, float, bool))})
    for r in st.db.execute(
            "SELECT r.src, r.rel, r.dst, r.confidence, r.evidence, s.file_path, c.locator FROM relations r "
            "JOIN chunks c ON c.id = r.chunk_id JOIN sources s ON s.id = c.source_id"):
        if r["src"] in G and r["dst"] in G and not G.has_edge(r["src"], r["dst"]):
            G.add_edge(r["src"], r["dst"], relation=r["rel"], confidence=r["confidence"],
                       source_file=r["file_path"].split("#", 1)[0], source_location=r["locator"],
                       _src=r["src"], _tgt=r["dst"])
    if include_inferred_mentions:
        # document -> mentions -> entity, from verbatim name matches.
        for r in st.db.execute(
                "SELECT DISTINCT s.doc_entity, m.entity_id, s.file_path, c.locator FROM mentions m "
                "JOIN chunks c ON c.id = m.chunk_id JOIN sources s ON s.id = c.source_id "
                "WHERE m.method = 'name_match'"):
            a, b = r["doc_entity"], r["entity_id"]
            if a in G and b in G and a != b and not G.has_edge(a, b):
                G.add_edge(a, b, relation="mentions", confidence="INFERRED", confidence_score=0.75,
                           source_file=r["file_path"].split("#", 1)[0], source_location=r["locator"],
                           _src=a, _tgt=b)
    return G


def export(st: MemoryStore, out_dir: str | Path, *, html: bool = True, **kw) -> dict:
    from graphify.cluster import cluster
    from graphify.export import to_json

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    G = build_graph(st, **kw)
    communities = cluster(G) if G.number_of_nodes() else {}
    labels = {}
    for cid, members in communities.items():
        best = max(members, key=lambda n: G.degree(n))
        labels[cid] = G.nodes[best].get("label", str(best))
    graph_path = out / "graph.json"
    to_json(G, communities, str(graph_path), force=True, community_labels=labels)
    result = {"graph_json": str(graph_path), "nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
              "communities": len(communities)}
    if html and G.number_of_nodes():
        from graphify.export import to_html

        html_path = out / "graph.html"
        to_html(G, communities, str(html_path), community_labels=labels)
        result["graph_html"] = str(html_path)
    return result
