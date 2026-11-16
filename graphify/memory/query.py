"""Read-side API: every result carries references back to the original source.

All functions return plain JSON-serialisable dicts/lists so the CLI, the MCP
server and any other agent see identical shapes.
"""
from __future__ import annotations

import json
import re

from graphify.memory.model import normalize
from graphify.memory.store import MemoryStore

_TOKEN = re.compile(r"[\w][\w\-.@']*", re.UNICODE)


def _ref(row) -> dict:
    """The provenance block attached to every hit."""
    return {
        "file": row["file_path"].split("#", 1)[0],
        "unit": row["unit"] or None,
        "locator": row["locator"],
        "kind": row["kind"],
        "source_title": row["source_title"],
        "timestamp": row["ts"],
    }


def _fts_query(text: str, mode: str) -> str:
    terms = [t.replace('"', '') for t in _TOKEN.findall(text)]
    terms = [t for t in terms if t]
    if not terms:
        return ""
    joiner = " OR " if mode == "any" else " "
    return joiner.join(f'"{t}"' for t in terms)


_CHUNK_SELECT = """
SELECT c.id AS chunk_id, c.locator, c.title, c.ts, c.text, c.meta,
       s.file_path, s.unit, s.kind, s.title AS source_title, s.doc_entity
FROM chunks c JOIN sources s ON s.id = c.source_id
"""


def _entities_for_chunks(st: MemoryStore, chunk_ids: list[int]) -> dict[int, list[dict]]:
    if not chunk_ids:
        return {}
    ph = ",".join("?" * len(chunk_ids))
    out: dict[int, list[dict]] = {}
    for r in st.db.execute(
            f"SELECT m.chunk_id, e.id, e.type, e.name, m.confidence, m.method FROM mentions m "
            f"JOIN entities e ON e.id = m.entity_id WHERE m.chunk_id IN ({ph}) AND m.method != 'source' "
            f"ORDER BY e.type, e.name", chunk_ids):
        out.setdefault(r["chunk_id"], []).append(
            {"id": r["id"], "type": r["type"], "name": r["name"], "confidence": r["confidence"]})
    return out


def search(st: MemoryStore, query: str, *, limit: int = 10, kind: str | None = None,
           entity: str | None = None, since: str | None = None, until: str | None = None,
           path_contains: str | None = None, mode: str = "all") -> dict:
    """Keyword search (SQLite FTS5 / BM25). ``mode='all'`` requires every term;
    if that finds nothing it retries with any term and says so."""
    where, params = [], []
    if kind:
        where.append("s.kind = ?")
        params.append(kind)
    if since:
        where.append("c.ts >= ?")
        params.append(since)
    if until:
        where.append("c.ts <= ?")
        params.append(until)
    if path_contains:
        where.append("s.file_path LIKE ?")
        params.append(f"%{path_contains}%")
    if entity:
        eid = _resolve_entity_id(st, entity)
        if not eid:
            return {"query": query, "results": [], "note": f"no entity matches {entity!r}"}
        where.append("c.id IN (SELECT chunk_id FROM mentions WHERE entity_id = ?)")
        params.append(eid)
    used_mode = mode
    rows = []
    if st.fts and query.strip():
        for m in ([mode] if mode == "any" else ["all", "any"]):
            fq = _fts_query(query, m)
            if not fq:
                break
            sql = (_CHUNK_SELECT.replace("SELECT c.id", "SELECT bm25(chunks_fts, 2.0, 1.0) AS score, "
                                                        "snippet(chunks_fts, 1, '[', ']', ' … ', 24) AS snip, c.id")
                   + " JOIN chunks_fts f ON f.rowid = c.id WHERE chunks_fts MATCH ?"
                   + "".join(f" AND {w}" for w in where) + " ORDER BY score LIMIT ?")
            rows = list(st.db.execute(sql, [fq, *params, limit]))
            used_mode = m
            if rows:
                break
    elif query.strip():
        # SQLite without FTS5: substring match on every term, newest first.
        terms = _TOKEN.findall(query)
        sql = _CHUNK_SELECT.replace("SELECT c.id", "SELECT 0 AS score, NULL AS snip, c.id") + " WHERE 1=1"
        sql += "".join(" AND c.text LIKE ?" for _ in terms) + "".join(f" AND {w}" for w in where)
        sql += " ORDER BY c.ts DESC LIMIT ?"
        rows = list(st.db.execute(sql, [*(f"%{t}%" for t in terms), *params, limit]))
        used_mode = "like"
    ents = _entities_for_chunks(st, [r["chunk_id"] for r in rows])
    results = []
    for r in rows:
        snippet = r["snip"] or (r["text"][:300] + ("…" if len(r["text"]) > 300 else ""))
        results.append({
            "chunk_id": r["chunk_id"],
            "score": round(-float(r["score"]), 4) if r["score"] else None,
            "title": r["title"],
            "snippet": snippet,
            "source": _ref(r),
            "entities": ents.get(r["chunk_id"], []),
        })
    out = {"query": query, "match": used_mode, "count": len(results), "results": results}
    if used_mode == "any" and mode == "all":
        out["note"] = "no chunk contains every term; showing chunks with any term"
    return out


def get_chunk(st: MemoryStore, chunk_id: int) -> dict | None:
    r = st.db.execute(_CHUNK_SELECT + " WHERE c.id = ?", (chunk_id,)).fetchone()
    if not r:
        return None
    ents = _entities_for_chunks(st, [chunk_id])
    return {"chunk_id": chunk_id, "title": r["title"], "text": r["text"], "source": _ref(r),
            "meta": json.loads(r["meta"]) if r["meta"] else {}, "entities": ents.get(chunk_id, [])}


def _resolve_entity_id(st: MemoryStore, name_or_id: str, etype: str | None = None) -> str | None:
    if st.db.execute("SELECT 1 FROM entities WHERE id = ?", (name_or_id,)).fetchone():
        return name_or_id
    norm = normalize(name_or_id)
    sql = "SELECT a.entity_id FROM aliases a JOIN entities e ON e.id = a.entity_id WHERE a.alias_norm = ?"
    params = [norm]
    if etype:
        sql += " AND a.type = ?"
        params.append(etype)
    sql += (" ORDER BY CASE a.type WHEN 'person' THEN 0 WHEN 'project' THEN 1 WHEN 'company' THEN 2 "
            "WHEN 'location' THEN 3 WHEN 'concept' THEN 4 ELSE 5 END LIMIT 1")
    r = st.db.execute(sql, params).fetchone()
    return r["entity_id"] if r else None


def find_entities(st: MemoryStore, text: str, *, etype: str | None = None, limit: int = 10) -> list[dict]:
    """Exact alias matches first, then fuzzy (rapidfuzz) name matches."""
    from rapidfuzz import fuzz, process

    norm = normalize(text)
    sql = "SELECT a.alias, a.alias_norm, e.id, e.type, e.name FROM aliases a JOIN entities e ON e.id = a.entity_id"
    params: list = []
    if etype:
        sql += " WHERE e.type = ?"
        params.append(etype)
    rows = list(st.db.execute(sql, params))
    choices = {i: r["alias_norm"] for i, r in enumerate(rows)}
    hits = process.extract(norm, choices, scorer=fuzz.WRatio, limit=limit * 4, score_cutoff=70)
    best: dict[str, dict] = {}
    for _, score, idx in hits:
        r = rows[idx]
        s = 100.0 if r["alias_norm"] == norm else float(score)
        if r["id"] not in best or best[r["id"]]["score"] < s:
            best[r["id"]] = {"id": r["id"], "type": r["type"], "name": r["name"], "matched": r["alias"],
                             "score": round(s, 1)}
    return sorted(best.values(), key=lambda x: (-x["score"], x["name"]))[:limit]


def entity(st: MemoryStore, name_or_id: str, *, etype: str | None = None, mention_limit: int = 10) -> dict | None:
    eid = _resolve_entity_id(st, name_or_id, etype)
    if not eid:
        return None
    e = st.db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    aliases = [r["alias"] for r in st.db.execute("SELECT alias FROM aliases WHERE entity_id = ? ORDER BY alias", (eid,))]
    rows = list(st.db.execute(
        _CHUNK_SELECT + " JOIN mentions m ON m.chunk_id = c.id WHERE m.entity_id = ? "
        "GROUP BY c.id ORDER BY c.ts DESC, c.id DESC LIMIT ?", (eid, mention_limit)))
    total = st.db.execute("SELECT COUNT(DISTINCT chunk_id) AS n FROM mentions WHERE entity_id = ?", (eid,)).fetchone()["n"]
    mentions = [{"chunk_id": r["chunk_id"], "title": r["title"],
                 "snippet": r["text"][:240] + ("…" if len(r["text"]) > 240 else ""), "source": _ref(r)} for r in rows]
    return {"id": eid, "type": e["type"], "name": e["name"], "aliases": aliases,
            "attrs": json.loads(e["attrs"] or "{}"), "mention_count": total, "mentions": mentions,
            "relations": related(st, eid, limit=50)["relations"]}


def related(st: MemoryStore, name_or_id: str, *, rel: str | None = None, limit: int = 50,
            include_cooccurrence: bool = True) -> dict:
    """Explicit relationships (with evidence and source), plus optional
    co-occurrence (entities mentioned in the same chunks), which is reported
    separately because it is a statistical signal, not a stated relationship."""
    eid = _resolve_entity_id(st, name_or_id)
    if not eid:
        return {"entity": None, "relations": [], "cooccurring": []}
    params: list = [eid, eid]
    sql = ("SELECT r.src, r.rel, r.dst, r.confidence, r.method, r.evidence, c.locator, c.ts, "
           "s.file_path, s.unit, s.kind, s.title AS source_title, "
           "es.name AS src_name, es.type AS src_type, ed.name AS dst_name, ed.type AS dst_type "
           "FROM relations r JOIN chunks c ON c.id = r.chunk_id JOIN sources s ON s.id = c.source_id "
           "JOIN entities es ON es.id = r.src JOIN entities ed ON ed.id = r.dst "
           "WHERE (r.src = ? OR r.dst = ?)")
    if rel:
        sql += " AND r.rel = ?"
        params.append(rel)
    sql += " ORDER BY r.rel, ed.name LIMIT ?"
    params.append(limit)
    out = []
    seen = set()
    for r in st.db.execute(sql, params):
        key = (r["src"], r["rel"], r["dst"], r["file_path"])
        if key in seen:
            continue
        seen.add(key)
        outgoing = r["src"] == eid
        out.append({
            "direction": "out" if outgoing else "in",
            "relation": r["rel"],
            "other": {"id": r["dst"] if outgoing else r["src"],
                      "name": r["dst_name"] if outgoing else r["src_name"],
                      "type": r["dst_type"] if outgoing else r["src_type"]},
            "confidence": r["confidence"], "method": r["method"], "evidence": r["evidence"],
            "source": _ref(r),
        })
    co = []
    if include_cooccurrence:
        for r in st.db.execute(
                "SELECT e.id, e.type, e.name, COUNT(DISTINCT m2.chunk_id) AS n FROM mentions m1 "
                "JOIN mentions m2 ON m2.chunk_id = m1.chunk_id AND m2.entity_id != m1.entity_id "
                "JOIN entities e ON e.id = m2.entity_id "
                "WHERE m1.entity_id = ? AND e.type NOT IN ('task', 'decision', 'tag') "
                "GROUP BY e.id ORDER BY n DESC, e.name LIMIT 15", (eid,)):
            co.append({"id": r["id"], "type": r["type"], "name": r["name"], "shared_chunks": r["n"],
                       "confidence": "INFERRED"})
    return {"entity": eid, "relations": out, "cooccurring": co}


def list_entities(st: MemoryStore, *, etype: str | None = None, limit: int = 100, status: str | None = None) -> list[dict]:
    sql = ("SELECT e.id, e.type, e.name, e.attrs, (SELECT COUNT(DISTINCT chunk_id) FROM mentions m WHERE m.entity_id = e.id) AS n "
           "FROM entities e")
    params: list = []
    if etype:
        sql += " WHERE e.type = ?"
        params.append(etype)
    sql += " ORDER BY n DESC, e.name LIMIT ?"
    params.append(limit)
    out = []
    for r in st.db.execute(sql, params):
        attrs = json.loads(r["attrs"] or "{}")
        if status and attrs.get("status") != status:
            continue
        out.append({"id": r["id"], "type": r["type"], "name": r["name"], "mentions": r["n"], "attrs": attrs})
    return out


def timeline(st: MemoryStore, name_or_id: str | None = None, *, since: str | None = None,
             until: str | None = None, limit: int = 50) -> list[dict]:
    sql = _CHUNK_SELECT + " WHERE c.ts IS NOT NULL"
    params: list = []
    if name_or_id:
        eid = _resolve_entity_id(st, name_or_id)
        if not eid:
            return []
        sql += " AND c.id IN (SELECT chunk_id FROM mentions WHERE entity_id = ?)"
        params.append(eid)
    if since:
        sql += " AND c.ts >= ?"
        params.append(since)
    if until:
        sql += " AND c.ts <= ?"
        params.append(until)
    sql += " ORDER BY c.ts DESC LIMIT ?"
    params.append(limit)
    return [{"chunk_id": r["chunk_id"], "timestamp": r["ts"], "title": r["title"],
             "snippet": r["text"][:200], "source": _ref(r)} for r in st.db.execute(sql, params)]


def list_sources(st: MemoryStore, *, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT path, root, status, reason, size, ingested_at, deleted_at FROM files"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY path LIMIT ?"
    params.append(limit)
    return [dict(r) for r in st.db.execute(sql, params)]


def stats(st: MemoryStore) -> dict:
    one = lambda q, *a: st.db.execute(q, a).fetchone()[0]  # noqa: E731
    by_type = {r["type"]: r["n"] for r in st.db.execute("SELECT type, COUNT(*) AS n FROM entities GROUP BY type ORDER BY n DESC")}
    by_kind = {r["kind"]: r["n"] for r in st.db.execute("SELECT kind, COUNT(*) AS n FROM sources GROUP BY kind ORDER BY n DESC")}
    files = {r["status"]: r["n"] for r in st.db.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
    conf = {r["confidence"]: r["n"] for r in st.db.execute("SELECT confidence, COUNT(*) AS n FROM relations GROUP BY confidence")}
    last = st.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "db": str(st.path), "roots": st.roots(), "files": files, "sources_by_kind": by_kind,
        "chunks": one("SELECT COUNT(*) FROM chunks"), "entities": by_type,
        "relations": one("SELECT COUNT(*) FROM relations"), "relation_confidence": conf,
        "mentions": one("SELECT COUNT(*) FROM mentions WHERE method != 'source'"),
        "full_text_search": "fts5" if st.fts else "like-fallback",
        "last_run": dict(last) if last else None,
    }


def runs(st: MemoryStore, *, limit: int = 10, run_id: int | None = None, log_limit: int = 200) -> dict:
    rs = [dict(r) for r in st.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
    target = run_id or (rs[0]["id"] if rs else None)
    log = [dict(r) for r in st.db.execute(
        "SELECT ts, level, path, action, message FROM run_log WHERE run_id = ? ORDER BY rowid LIMIT ?",
        (target, log_limit))] if target else []
    return {"runs": rs, "log_for_run": target, "log": log}


def dedup_candidates(st: MemoryStore, *, etype: str | None = None, threshold: float = 90.0, limit: int = 50) -> list[dict]:
    """Pairs of same-type entities with very similar names, for human review.
    Nothing is merged automatically."""
    from rapidfuzz import fuzz

    sql = "SELECT id, type, name FROM entities WHERE type IN ('person','project','company','location','concept')"
    params: list = []
    if etype:
        sql = "SELECT id, type, name FROM entities WHERE type = ?"
        params.append(etype)
    rows = list(st.db.execute(sql, params))
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    out = []
    for t, items in by_type.items():
        items = items[:3000]
        for i, a in enumerate(items):
            na = normalize(a["name"])
            for b in items[i + 1:]:
                nb = normalize(b["name"])
                if abs(len(na) - len(nb)) > max(len(na), len(nb)) * 0.5:
                    continue
                s = fuzz.token_sort_ratio(na, nb)
                if s >= threshold:
                    out.append({"type": t, "a": {"id": a["id"], "name": a["name"]},
                                "b": {"id": b["id"], "name": b["name"]}, "similarity": round(s, 1)})
    out.sort(key=lambda x: -x["similarity"])
    return out[:limit]
