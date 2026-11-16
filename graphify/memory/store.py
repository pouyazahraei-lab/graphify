"""SQLite persistence for graphify.memory.

One file, stdlib ``sqlite3`` only, WAL journal. Every source is written inside
its own transaction, so an interrupted ingest leaves each source either fully
old or fully new, never half-written.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from graphify.memory.model import EntityRef, ParsedSource, normalize

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS roots (
    path TEXT PRIMARY KEY, added_at TEXT NOT NULL, label TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, root TEXT NOT NULL, size INTEGER, mtime REAL, sha256 TEXT,
    status TEXT NOT NULL,            -- active | skipped | error | deleted
    reason TEXT, first_seen TEXT, ingested_at TEXT, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, unit TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT, ts TEXT, doc_entity TEXT, meta TEXT,
    ingested_at TEXT NOT NULL, UNIQUE(file_path, unit)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, locator TEXT NOT NULL, title TEXT, ts TEXT, text TEXT NOT NULL, meta TEXT
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, attrs TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entities_type ON entities(type);
CREATE TABLE IF NOT EXISTS aliases (
    type TEXT NOT NULL, alias_norm TEXT NOT NULL, entity_id TEXT NOT NULL, alias TEXT NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(type, alias_norm)
);
CREATE INDEX IF NOT EXISTS aliases_entity ON aliases(entity_id);
CREATE TABLE IF NOT EXISTS mentions (
    entity_id TEXT NOT NULL, chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    method TEXT NOT NULL, confidence TEXT NOT NULL, PRIMARY KEY(entity_id, chunk_id, method)
);
CREATE INDEX IF NOT EXISTS mentions_chunk ON mentions(chunk_id);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    method TEXT NOT NULL, confidence TEXT NOT NULL, evidence TEXT,
    UNIQUE(src, rel, dst, chunk_id, method)
);
CREATE INDEX IF NOT EXISTS relations_src ON relations(src);
CREATE INDEX IF NOT EXISTS relations_dst ON relations(dst);
CREATE TABLE IF NOT EXISTS links (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    src TEXT NOT NULL, target TEXT NOT NULL, target_norm TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_chunk ON links(chunk_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
    unchanged INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
    run_id INTEGER NOT NULL, ts TEXT NOT NULL, level TEXT NOT NULL,
    path TEXT, action TEXT, message TEXT
);
"""

_FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, text, tokenize='porter unicode61 remove_diacritics 2')"

# Name-matching entity types: the ones whose names are proper nouns worth
# finding in free text. Tasks/decisions/tags/documents are not matched.
MATCHABLE_TYPES = ("person", "project", "company", "location", "concept", "repository")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    env = os.environ.get("GRAPHIFY_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".graphify" / "memory" / "memory.db"


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(_SCHEMA)
        try:
            self.db.execute(_FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False     # SQLite built without FTS5: search falls back to LIKE
        self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # ------------------------------------------------------------------ basics
    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value))

    class _Tx:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, exc_type, exc, tb):
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
            return False

    def tx(self):
        return MemoryStore._Tx(self.db)

    # ------------------------------------------------------------------ roots / runs
    def add_root(self, path: Path, label: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO roots(path, added_at, label) VALUES (?, ?, ?)", (str(path), now(), label))

    def remove_root(self, path: Path) -> bool:
        cur = self.db.execute("DELETE FROM roots WHERE path = ?", (str(path),))
        return cur.rowcount > 0

    def roots(self) -> list[str]:
        return [r["path"] for r in self.db.execute("SELECT path FROM roots ORDER BY path")]

    def start_run(self, note: str = "") -> int:
        # A run left "running" means a previous process died mid-ingest. Its
        # committed sources are intact; mark it so the log tells the truth.
        self.db.execute("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (now(),))
        cur = self.db.execute("INSERT INTO runs(started_at, status, note) VALUES (?, 'running', ?)", (now(), note))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, counts: dict, status: str = "ok") -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, added=?, updated=?, deleted=?, unchanged=?, skipped=?, errors=? WHERE id=?",
            (now(), status, counts.get("added", 0), counts.get("updated", 0), counts.get("deleted", 0),
             counts.get("unchanged", 0), counts.get("skipped", 0), counts.get("errors", 0), run_id))

    def log(self, run_id: int, level: str, path: str | None, action: str, message: str = "") -> None:
        self.db.execute("INSERT INTO run_log(run_id, ts, level, path, action, message) VALUES (?, ?, ?, ?, ?, ?)",
                        (run_id, now(), level, path, action, message[:2000]))

    # ------------------------------------------------------------------ files
    def file_row(self, path: str):
        return self.db.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def files_under(self, root: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM files WHERE root = ? AND status != 'deleted'", (root,)))

    def mark_file(self, db, path: str, root: str, size: int, mtime: float, sha: str, status: str, reason: str = "") -> None:
        t = now()
        db.execute(
            """INSERT INTO files(path, root, size, mtime, sha256, status, reason, first_seen, ingested_at, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(path) DO UPDATE SET root=excluded.root, size=excluded.size, mtime=excluded.mtime,
                 sha256=excluded.sha256, status=excluded.status, reason=excluded.reason,
                 ingested_at=excluded.ingested_at, deleted_at=NULL""",
            (path, root, size, mtime, sha, status, reason, t, t))

    def touch_file_stat(self, path: str, size: int, mtime: float) -> None:
        self.db.execute("UPDATE files SET size = ?, mtime = ? WHERE path = ?", (size, mtime, path))

    # ------------------------------------------------------------------ entities
    def resolve(self, db, ref: EntityRef) -> str:
        """Find or create the entity for ref and register its aliases.

        Identity rules, most specific first:
        1. the ref's own id (type + key, e.g. ``person:email:a@b.com``);
        2. a manual alias (a merge the user made) for any of its names;
        3. for unkeyed refs, an existing alias of the same type;
        4. for email-keyed people, an existing person with the same *full*
           display name (two words or more) and no conflicting email.
        Single-word display names never merge people automatically."""
        own_id = ref.identity()
        names = [n for n in [ref.name] + list(ref.aliases) if n]
        eid = None
        if db.execute("SELECT 1 FROM entities WHERE id = ?", (own_id,)).fetchone():
            eid = own_id
        if eid is None:
            for n in names:
                hit = db.execute("SELECT entity_id FROM aliases WHERE type = ? AND alias_norm = ? AND manual = 1",
                                 (ref.type, normalize(n))).fetchone()
                if hit:
                    eid = hit["entity_id"]
                    break
        if eid is None and ref.key is None:
            for n in names:
                hit = db.execute("SELECT entity_id FROM aliases WHERE type = ? AND alias_norm = ?",
                                 (ref.type, normalize(n))).fetchone()
                if hit:
                    eid = hit["entity_id"]
                    break
        email_addr = (ref.attrs or {}).get("email")
        if eid is None and ref.type == "person" and email_addr:
            display = ref.name if "@" not in ref.name else ""
            if len(display.split()) >= 2:
                hit = db.execute("SELECT a.entity_id, e.attrs FROM aliases a JOIN entities e ON e.id = a.entity_id "
                                 "WHERE a.type = 'person' AND a.alias_norm = ?", (normalize(display),)).fetchone()
                if hit:
                    other = json.loads(hit["attrs"] or "{}").get("email")
                    if other in (None, "", email_addr):
                        eid = hit["entity_id"]
        if eid is None:
            eid = own_id
        t = now()
        exists = db.execute("SELECT attrs, name FROM entities WHERE id = ?", (eid,)).fetchone()
        if exists is None:
            db.execute("INSERT INTO entities(id, type, name, attrs, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (eid, ref.type, ref.name, json.dumps(ref.attrs or {}, ensure_ascii=False), t, t))
        else:
            attrs = json.loads(exists["attrs"] or "{}")
            merged = dict(attrs)
            for k, v in (ref.attrs or {}).items():
                if v in (None, ""):
                    continue
                if k == "placeholder":
                    continue
                merged[k] = v
            if ref.method != "wikilink":
                merged.pop("placeholder", None)   # a real mention upgrades a placeholder
            name = exists["name"]
            if "@" in name and ref.name and "@" not in ref.name:
                name = ref.name                    # prefer a display name over an address
            if merged != attrs or name != exists["name"]:
                db.execute("UPDATE entities SET attrs = ?, name = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(merged, ensure_ascii=False), name, t, eid))
        for n in names:
            norm = normalize(n)
            if not norm:
                continue
            if (ref.type == "person" and (ref.key or "").startswith("email:")
                    and "@" not in n and len(n.split()) < 2):
                continue                           # single-word display names are too weak to index
            db.execute("INSERT OR IGNORE INTO aliases(type, alias_norm, entity_id, alias) VALUES (?, ?, ?, ?)",
                       (ref.type, norm, eid, n))
        return eid

    def merge_entities(self, keep: str, drop: str) -> dict:
        """Manually merge ``drop`` into ``keep``; recorded as manual aliases so
        re-ingestion keeps them merged."""
        with self.tx() as db:
            k = db.execute("SELECT * FROM entities WHERE id = ?", (keep,)).fetchone()
            d = db.execute("SELECT * FROM entities WHERE id = ?", (drop,)).fetchone()
            if not k or not d:
                raise ValueError("both entities must exist")
            if k["type"] != d["type"]:
                raise ValueError(f"cannot merge {d['type']} into {k['type']}")
            db.execute("UPDATE OR IGNORE mentions SET entity_id = ? WHERE entity_id = ?", (keep, drop))
            db.execute("DELETE FROM mentions WHERE entity_id = ?", (drop,))
            db.execute("UPDATE OR IGNORE relations SET src = ? WHERE src = ?", (keep, drop))
            db.execute("UPDATE OR IGNORE relations SET dst = ? WHERE dst = ?", (keep, drop))
            db.execute("DELETE FROM relations WHERE src = ? OR dst = ?", (drop, drop))
            db.execute("UPDATE links SET src = ? WHERE src = ?", (keep, drop))
            db.execute("UPDATE aliases SET entity_id = ?, manual = 1 WHERE entity_id = ?", (keep, drop))
            db.execute("INSERT OR REPLACE INTO aliases(type, alias_norm, entity_id, alias, manual) VALUES (?, ?, ?, ?, 1)",
                       (d["type"], normalize(d["name"]), keep, d["name"]))
            key = drop.split(":", 1)[1]
            db.execute("INSERT OR REPLACE INTO aliases(type, alias_norm, entity_id, alias, manual) VALUES (?, ?, ?, ?, 1)",
                       (d["type"], key, keep, key))
            db.execute("DELETE FROM entities WHERE id = ?", (drop,))
        return {"kept": keep, "merged": drop}

    # ------------------------------------------------------------------ sources
    def delete_file_sources(self, db, file_path: str) -> int:
        ids = [r["id"] for r in db.execute("SELECT id FROM sources WHERE file_path = ?", (file_path,))]
        for sid in ids:
            if self.fts:
                db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE source_id = ?)", (sid,))
            db.execute("DELETE FROM sources WHERE id = ?", (sid,))
        return len(ids)

    def write_source(self, db, file_path: str, unit: str, parsed: ParsedSource) -> int:
        doc_id = self.resolve(db, parsed.doc)
        cur = db.execute(
            "INSERT INTO sources(file_path, unit, kind, title, ts, doc_entity, meta, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_path, unit, parsed.kind, parsed.title, parsed.ts, doc_id,
             json.dumps(parsed.meta, ensure_ascii=False, default=str), now()))
        sid = int(cur.lastrowid)
        for ordinal, chunk in enumerate(parsed.chunks):
            c = db.execute(
                "INSERT INTO chunks(source_id, ordinal, locator, title, ts, text, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, ordinal, chunk.locator, chunk.title, chunk.ts or parsed.ts, chunk.text,
                 json.dumps(chunk.meta, ensure_ascii=False, default=str) if chunk.meta else None))
            cid = int(c.lastrowid)
            if self.fts:
                db.execute("INSERT INTO chunks_fts(rowid, title, text) VALUES (?, ?, ?)", (cid, chunk.title or "", chunk.text))
            db.execute("INSERT OR IGNORE INTO mentions(entity_id, chunk_id, method, confidence) VALUES (?, ?, 'source', 'EXTRACTED')",
                       (doc_id, cid))
            for ent in chunk.entities:
                eid = self.resolve(db, ent)
                db.execute("INSERT OR IGNORE INTO mentions(entity_id, chunk_id, method, confidence) VALUES (?, ?, ?, ?)",
                           (eid, cid, ent.method, ent.confidence))
            for rel in chunk.relations:
                s = doc_id if rel.src is parsed.doc else self.resolve(db, rel.src)
                d = doc_id if rel.dst is parsed.doc else self.resolve(db, rel.dst)
                if s != d:
                    db.execute("INSERT OR IGNORE INTO relations(src, rel, dst, chunk_id, method, confidence, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (s, rel.rel, d, cid, rel.method, rel.confidence, rel.evidence))
            for target in chunk.links:
                db.execute("INSERT INTO links(chunk_id, src, target, target_norm) VALUES (?, ?, ?, ?)",
                           (cid, doc_id, target, normalize(Path(target).name if "/" in target else target)))
        return sid

    # ------------------------------------------------------------------ derived passes
    def resolve_links(self) -> dict:
        """Turn [[wikilinks]] into links_to relations. Recomputed from scratch
        each run, so a renamed or newly created note re-targets old links."""
        with self.tx() as db:
            db.execute("DELETE FROM relations WHERE method = 'wikilink'")
            # Note titles: the doc entity of every markdown source (by title and
            # file stem), plus aliases of any non-placeholder entity.
            index: dict[str, str] = {}
            for r in db.execute("SELECT s.doc_entity, s.title, s.file_path FROM sources s WHERE s.kind IN ('markdown','docx','text')"):
                for k in (normalize(r["title"] or ""), normalize(Path(r["file_path"]).stem)):
                    if k:
                        index.setdefault(k, r["doc_entity"])
            for r in db.execute("SELECT a.alias_norm, a.entity_id FROM aliases a JOIN entities e ON e.id = a.entity_id "
                                "WHERE a.type IN ('person','project','company','location','concept','event')"):
                index.setdefault(r["alias_norm"], r["entity_id"])
            resolved = unresolved = 0
            for r in list(db.execute("SELECT chunk_id, src, target, target_norm FROM links")):
                dst = index.get(r["target_norm"])
                if dst is None:
                    # Obsidian treats an unresolved link as a note-to-be; keep
                    # it as a concept placeholder rather than dropping it.
                    dst = self.resolve(db, EntityRef(type="concept", name=r["target"].strip(),
                                                     attrs={"placeholder": True}, method="wikilink"))
                    unresolved += 1
                else:
                    resolved += 1
                if dst != r["src"]:
                    db.execute("INSERT OR IGNORE INTO relations(src, rel, dst, chunk_id, method, confidence, evidence) "
                               "VALUES (?, 'links_to', ?, ?, 'wikilink', 'EXTRACTED', ?)",
                               (r["src"], dst, r["chunk_id"], f"[[{r['target']}]]"))
        return {"resolved": resolved, "unresolved": unresolved}

    def match_names(self, changed_sources: set[int] | None, min_len: int = 4) -> int:
        """INFERRED mentions: known entity names/aliases found verbatim (word
        boundaries, case-insensitive) in chunk text. Runs over all chunks when
        the set of names changed, otherwise only over changed sources."""
        import re

        rows = list(self.db.execute(
            f"SELECT a.alias, a.entity_id FROM aliases a JOIN entities e ON e.id = a.entity_id "
            f"WHERE a.type IN ({','.join('?' * len(MATCHABLE_TYPES))}) "
            f"AND (e.attrs IS NULL OR e.attrs NOT LIKE '%\"placeholder\": true%')", MATCHABLE_TYPES))
        by_name: dict[str, set[str]] = {}
        for r in rows:
            alias = r["alias"].strip()
            if len(alias) < min_len or "@" in alias:
                continue
            by_name.setdefault(alias.casefold(), set()).add(r["entity_id"])
        # A name shared by several entities is ambiguous: do not guess.
        names = {n: next(iter(ids)) for n, ids in by_name.items() if len(ids) == 1}
        signature = hashlib.sha256(json.dumps(sorted(names.items())).encode("utf-8")).hexdigest()
        full = changed_sources is None or self.get_meta("name_signature") != signature
        with self.tx() as db:
            if full:
                db.execute("DELETE FROM mentions WHERE method = 'name_match'")
                chunk_rows = db.execute("SELECT id, text FROM chunks")
            else:
                if not changed_sources:
                    self.set_meta("name_signature", signature)
                    return 0
                ph = ",".join("?" * len(changed_sources))
                chunk_rows = db.execute(f"SELECT id, text FROM chunks WHERE source_id IN ({ph})", tuple(changed_sources))
            chunk_rows = list(chunk_rows)
            count = 0
            if names:
                ordered = sorted(names, key=len, reverse=True)
                pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in ordered) + r")(?!\w)", re.I)
                for row in chunk_rows:
                    found = {names[m.group(1).casefold()] for m in pattern.finditer(row["text"])
                             if m.group(1).casefold() in names}
                    for eid in found:
                        cur = db.execute("INSERT OR IGNORE INTO mentions(entity_id, chunk_id, method, confidence) "
                                         "SELECT ?, ?, 'name_match', 'INFERRED' WHERE NOT EXISTS "
                                         "(SELECT 1 FROM mentions WHERE entity_id = ? AND chunk_id = ?)",
                                         (eid, row["id"], eid, row["id"]))
                        count += cur.rowcount
            db.execute("INSERT INTO meta(key, value) VALUES ('name_signature', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (signature,))
        return count

    def garbage_collect(self) -> int:
        """Drop entities no longer supported by any chunk or relation (their
        sources were deleted). Manually merged aliases are kept."""
        with self.tx() as db:
            cur = db.execute(
                "DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions) "
                "AND id NOT IN (SELECT src FROM relations) AND id NOT IN (SELECT dst FROM relations)")
            removed = cur.rowcount
            db.execute("DELETE FROM aliases WHERE manual = 0 AND entity_id NOT IN (SELECT id FROM entities)")
        return removed
