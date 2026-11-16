"""Source adapters: turn one file into a ParsedSource of attributed chunks.

Every adapter is offline and deterministic. Entities are only emitted when the
source states them explicitly (frontmatter, email headers, task checkboxes,
lines marked "Decision:", commit authors). Free-text name matching happens
later, against entities that already exist (see ingest.py), and is labelled
INFERRED.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import json
import mailbox
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

from graphify.memory.model import (
    FRONTMATTER_LINK_KEYS,
    Chunk,
    EntityRef,
    ParsedSource,
    RelationRef,
    canonical_type,
    clean_value,
    normalize,
)

MAX_CHUNK_CHARS = 2000
MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024
GIT_LOG_LIMIT = 1000

TEXT_EXTS = {".txt", ".text", ".log", ".rst"}
MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
SUPPORTED_EXTS = TEXT_EXTS | MARKDOWN_EXTS | {".pdf", ".docx", ".xlsx", ".eml", ".mbox", ".json", ".zip"}


class SkipSource(Exception):
    """The file is not something this layer ingests (e.g. an unrelated .json)."""


# --------------------------------------------------------------------------- helpers

def iso_from_epoch(value) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds")
    if hasattr(value, "isoformat"):          # datetime.date from YAML
        return value.isoformat()
    text = str(value).strip()
    return text or None


def split_text(text: str, start_line: int = 1, limit: int = MAX_CHUNK_CHARS) -> list[tuple[int, int, str]]:
    """Split text into paragraph-aligned pieces of at most ~limit chars.

    Returns (first_line, last_line, text) with 1-based line numbers relative to
    the original file (offset by start_line - 1)."""
    lines = text.split("\n")
    pieces: list[tuple[int, int, str]] = []
    buf: list[str] = []
    buf_start = 0
    size = 0
    for i, line in enumerate(lines):
        if not buf:
            buf_start = i
        buf.append(line)
        size += len(line) + 1
        paragraph_end = line.strip() == ""
        if size >= limit and (paragraph_end or size >= limit * 1.5):
            body = "\n".join(buf).strip()
            if body:
                last = i
                while last > buf_start and not lines[last].strip():
                    last -= 1
                pieces.append((start_line + buf_start, start_line + last, body))
            buf, size = [], 0
    body = "\n".join(buf).strip()
    if body:
        last = len(lines) - 1
        while last > buf_start and not lines[last].strip():
            last -= 1
        pieces.append((start_line + buf_start, start_line + last, body))
    return pieces


def _doc_ref(kind: str, title: str, path_key: str) -> EntityRef:
    etype = {"chatgpt": "conversation", "git": "repository"}.get(kind, "document")
    return EntityRef(type=etype, name=title, key=normalize(path_key), method="source")


# --------------------------------------------------------------------------- markdown

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#\n]+?)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
_TAG = re.compile(r"(?<![\w&/#])#([A-Za-z][\w\-/]{1,60})")
_TASK = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$")
_DECISION = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(?:decision|decided|we decided|i decided|resolution)(?:\*\*|__)?"
    r"\s*(?:\*\*|__)?\s*[:\-–—]\s*(?:\*\*|__)?\s*(.+?)\s*$",
    re.I,
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_CODE_FENCE = re.compile(r"^\s*(```|~~~)")


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """Return (frontmatter dict, body, number of lines consumed)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text, 0
    try:
        import yaml

        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    consumed = text[: m.end()].count("\n")
    return data, text[m.end():], consumed


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, "")]
    if isinstance(value, str) and "," in value and "[[" not in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


_WIKILINK_DISPLAY = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]")


def _plain(text: str) -> str:
    """Display text for a task/decision: ``[[Note|alias]]`` -> ``alias``."""
    return _WIKILINK_DISPLAY.sub(lambda m: (m.group(2) or m.group(1)).strip(), text).strip()


def _markdown_entities(lines: list[str], first_line: int, note: EntityRef, path_key: str,
                       in_decision_section: bool) -> tuple[list[EntityRef], list[RelationRef], list[str]]:
    ents: list[EntityRef] = []
    rels: list[RelationRef] = []
    links: list[str] = []
    in_code = False
    for offset, line in enumerate(lines):
        if _CODE_FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        lineno = first_line + offset
        for target in _WIKILINK.findall(line):
            target = target.strip()
            if target:
                links.append(target)
        for tag in _TAG.findall(line):
            t = EntityRef(type="tag", name=tag.lower())
            ents.append(t)
            rels.append(RelationRef(note, "tagged", t, evidence=f"L{lineno}"))
        task = _TASK.match(line)
        if task:
            done = task.group(1).lower() == "x"
            label = _plain(task.group(2))
            t = EntityRef(type="task", name=label[:200],
                          key=normalize(f"{path_key}#{label}"),
                          attrs={"status": "done" if done else "open"})
            ents.append(t)
            rels.append(RelationRef(note, "has_task", t, evidence=f"L{lineno}"))
            continue
        dec = _DECISION.match(line)
        item = _LIST_ITEM.match(line) if in_decision_section and not dec else None
        text = dec.group(1) if dec else (item.group(1) if item else None)
        text = _plain(text) if text else None
        if text:
            d = EntityRef(type="decision", name=text[:300], key=normalize(f"{path_key}#{text}"))
            ents.append(d)
            rels.append(RelationRef(note, "records_decision", d, evidence=f"L{lineno}"))
    return ents, rels, links


def parse_markdown_text(text: str, *, path_key: str, default_title: str, kind: str = "markdown",
                        line_offset: int = 0) -> ParsedSource:
    fm, body, consumed = parse_frontmatter(text)
    title = clean_value(fm.get("title")) or default_title
    etype = canonical_type(fm.get("type") or fm.get("category"))
    if etype in (None, "document", "tag"):
        note = _doc_ref(kind, title, path_key)
    else:
        # An Obsidian "entity note": the note IS the person/project/... it
        # describes, keyed by title so links and mentions converge on it.
        note = EntityRef(type=etype, name=title, method="frontmatter")
    note.aliases = [clean_value(a) for a in _as_list(fm.get("aliases") or fm.get("alias")) if clean_value(a)]
    if etype is None and fm.get("type"):
        note.attrs["declared_type"] = str(fm.get("type"))
    for key in ("status", "role", "email", "url", "start", "end", "due"):
        if key in fm and not isinstance(fm[key], (dict, list)):
            note.attrs[key] = _iso(fm[key])
    ts = _iso(fm.get("date") or fm.get("created") or fm.get("updated"))

    fm_ents: list[EntityRef] = []
    fm_rels: list[RelationRef] = []
    for key, value in fm.items():
        spec = FRONTMATTER_LINK_KEYS.get(str(key).lower())
        if not spec:
            continue
        ftype, rel = spec
        for item in _as_list(value):
            name = clean_value(item)
            if not name:
                continue
            ref = EntityRef(type=ftype, name=name.lower() if ftype == "tag" else name, method="frontmatter")
            fm_ents.append(ref)
            fm_rels.append(RelationRef(note, rel, ref, evidence=f"frontmatter:{key}", method="frontmatter"))

    # Sections by heading, then size-limited pieces.
    body_lines = body.split("\n")
    first = consumed + 1 + line_offset
    sections: list[tuple[str, int, list[str]]] = []
    current_title, current_start, current = title, first, []
    in_code = False
    for i, line in enumerate(body_lines):
        if _CODE_FENCE.match(line):
            in_code = not in_code
        h = None if in_code else _HEADING.match(line)
        if h and current and any(x.strip() for x in current):
            sections.append((current_title, current_start, current))
            current_title, current_start, current = h.group(2).strip(), first + i, [line]
        else:
            if h:
                current_title = h.group(2).strip()
            current.append(line)
    if current and any(x.strip() for x in current):
        sections.append((current_title, current_start, current))

    chunks: list[Chunk] = []
    for sec_title, sec_start, sec_lines in sections:
        decision_section = "decision" in sec_title.lower()
        for a, b, piece in split_text("\n".join(sec_lines), start_line=sec_start):
            piece_lines = piece.split("\n")
            ents, rels, links = _markdown_entities(piece_lines, a, note, path_key, decision_section)
            chunks.append(Chunk(locator=f"L{a}-L{b}", text=piece, title=sec_title, ts=ts,
                                entities=ents, relations=rels, links=links))
    if not chunks:
        chunks.append(Chunk(locator="L1", text=title, title=title, ts=ts))
    chunks[0].entities[:0] = fm_ents
    chunks[0].relations[:0] = fm_rels
    meta = {k: _iso(v) if not isinstance(v, (list, dict)) else v for k, v in fm.items()
            if isinstance(k, str) and k.lower() not in ("password", "token", "secret", "api_key")}
    return ParsedSource(kind=kind, title=title, doc=note, chunks=chunks, ts=ts, meta={"frontmatter": meta} if meta else {})


def parse_markdown(path: Path, path_key: str) -> ParsedSource:
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_markdown_text(text, path_key=path_key, default_title=path.stem)


# --------------------------------------------------------------------------- text / pdf / office

def parse_text(path: Path, path_key: str) -> ParsedSource:
    text = path.read_text(encoding="utf-8", errors="replace")
    doc = _doc_ref("text", path.stem, path_key)
    chunks = [Chunk(locator=f"L{a}-L{b}", text=t, title=path.stem) for a, b, t in split_text(text)]
    return ParsedSource(kind="text", title=path.stem, doc=doc, chunks=chunks or [Chunk("L1", "", path.stem)])


def parse_pdf(path: Path, path_key: str) -> ParsedSource:
    from graphify.detect import _file_within_size_cap

    if not _file_within_size_cap(path):
        raise SkipSource("PDF exceeds graphify's size cap")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SkipSource('pypdf not installed: pip install "graphifyy[pdf]"') from exc
    reader = PdfReader(str(path))
    title = path.stem
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip() or title
    except Exception:
        pass
    chunks: list[Chunk] = []
    for pageno, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        for i, (_, _, piece) in enumerate(split_text(text)):
            loc = f"p.{pageno}" + (f"#{i + 1}" if i else "")
            chunks.append(Chunk(locator=loc, text=piece, title=title))
    if not chunks:
        raise SkipSource("PDF has no extractable text (scanned? OCR is not implemented)")
    return ParsedSource(kind="pdf", title=title, doc=_doc_ref("pdf", title, path_key), chunks=chunks,
                        meta={"pages": len(reader.pages)})


def parse_docx(path: Path, path_key: str) -> ParsedSource:
    from graphify.detect import docx_to_markdown

    md = docx_to_markdown(path)
    if not md.strip():
        raise SkipSource('empty .docx or python-docx missing: pip install "graphifyy[office]"')
    parsed = parse_markdown_text(md, path_key=path_key, default_title=path.stem, kind="docx")
    # Line numbers refer to the converted markdown, not Word pages; say so.
    for c in parsed.chunks:
        c.locator = f"section:{c.title}|{c.locator}"
    return parsed


def parse_xlsx(path: Path, path_key: str) -> ParsedSource:
    from graphify.detect import xlsx_to_markdown

    md = xlsx_to_markdown(path)
    if not md.strip():
        raise SkipSource('empty .xlsx or openpyxl missing: pip install "graphifyy[office]"')
    doc = _doc_ref("xlsx", path.stem, path_key)
    chunks: list[Chunk] = []
    sheet = path.stem
    for block in re.split(r"(?m)^(?=## )", md):
        if not block.strip():
            continue
        m = re.match(r"## (.+)", block)
        if m:
            sheet = re.sub(r"^Sheet:\s*", "", m.group(1).strip())
        for i, (a, b, piece) in enumerate(split_text(block)):
            chunks.append(Chunk(locator=f"sheet:{sheet}" + (f"#{i + 1}" if i else ""), text=piece, title=sheet))
    return ParsedSource(kind="xlsx", title=path.stem, doc=doc, chunks=chunks)


# --------------------------------------------------------------------------- email

def _addresses(msg: Message, header: str) -> list[tuple[str, str]]:
    values = msg.get_all(header, []) or []
    out = []
    for name, addr in email.utils.getaddresses([str(v) for v in values]):
        addr = (addr or "").strip().lower()
        if "@" in addr:
            out.append((name.strip(), addr))
    return out


def _person(name: str, addr: str) -> EntityRef:
    return EntityRef(type="person", name=name or addr, key=f"email:{addr}",
                     aliases=[a for a in (name, addr) if a], attrs={"email": addr}, method="email_header")


def _email_body(msg: Message) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            for part in msg.walk():
                if part.get_content_type() == "text/html" and not part.get_filename():
                    payload = part.get_payload(decode=True) or b""
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
            return ""
        payload = msg.get_payload(decode=True)
        if payload is None:
            return str(msg.get_payload() or "")
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        return text
    except Exception:
        return ""


def _email_chunks(msg: Message, locator_base: str, doc: EntityRef) -> list[Chunk]:
    subject = str(msg.get("Subject", "") or "(no subject)").strip()
    try:
        dt = email.utils.parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
    except (TypeError, ValueError):
        dt = None
    ts = _iso(dt)
    ents: list[EntityRef] = []
    rels: list[RelationRef] = []
    header_lines = []
    for header, rel in (("From", "sent_by"), ("To", "sent_to"), ("Cc", "cc")):
        addrs = _addresses(msg, header)
        if addrs:
            header_lines.append(f"{header}: " + ", ".join(f"{n} <{a}>" if n else a for n, a in addrs))
        for name, addr in addrs:
            p = _person(name, addr)
            ents.append(p)
            rels.append(RelationRef(doc, rel, p, evidence=f"header:{header}", method="email_header"))
    body = _email_body(msg)
    head = f"Subject: {subject}\n" + "\n".join(header_lines)
    meta = {"message_id": str(msg.get("Message-ID", "") or "").strip()}
    pieces = split_text(body) or [(1, 1, "")]
    chunks = []
    for i, (_, _, piece) in enumerate(pieces):
        text = (head + "\n\n" + piece).strip() if i == 0 else piece
        chunks.append(Chunk(locator=locator_base + (f"#{i + 1}" if i else ""), text=text, title=subject, ts=ts,
                            meta=meta, entities=ents if i == 0 else [], relations=rels if i == 0 else []))
    return chunks


def parse_eml(path: Path, path_key: str) -> ParsedSource:
    with path.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=email.policy.compat32)
    subject = str(msg.get("Subject", "") or path.stem).strip()
    doc = _doc_ref("email", subject, path_key)
    chunks = _email_chunks(msg, "message", doc)
    return ParsedSource(kind="email", title=subject, doc=doc, chunks=chunks, ts=chunks[0].ts if chunks else None)


def parse_mbox(path: Path, path_key: str) -> ParsedSource:
    box = mailbox.mbox(str(path), create=False)
    doc = _doc_ref("mbox", path.stem, path_key)
    chunks: list[Chunk] = []
    try:
        for n, msg in enumerate(box, start=1):
            mid = str(msg.get("Message-ID", "") or "").strip().strip("<>")
            chunks.extend(_email_chunks(msg, f"msg:{n}" + (f"<{mid}>" if mid else ""), doc))
    finally:
        box.close()
    return ParsedSource(kind="mbox", title=path.stem, doc=doc, chunks=chunks, meta={"messages": len(chunks)})


# --------------------------------------------------------------------------- ChatGPT export

def _looks_like_chatgpt(data) -> bool:
    return isinstance(data, list) and bool(data) and isinstance(data[0], dict) and "mapping" in data[0]


def _chatgpt_messages(conv: dict) -> list[dict]:
    """Messages on the conversation's current branch, oldest first."""
    mapping = conv.get("mapping") or {}
    node_id = conv.get("current_node")
    chain = []
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        chain.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    if not chain:   # no current_node: fall back to create_time order
        chain = sorted(mapping.values(), key=lambda n: ((n.get("message") or {}).get("create_time") or 0))
    else:
        chain.reverse()
    out = []
    for node in chain:
        msg = node.get("message") or {}
        role = ((msg.get("author") or {}).get("role")) or ""
        content = msg.get("content") or {}
        parts = content.get("parts") or []
        text = "\n".join(p for p in parts if isinstance(p, str)).strip()
        if not text and isinstance(content.get("text"), str):
            text = content["text"].strip()
        if role in ("user", "assistant") and text:
            out.append({"id": msg.get("id") or node.get("id"), "role": role, "text": text,
                        "ts": iso_from_epoch(msg.get("create_time"))})
    return out


def parse_chatgpt_data(data: list, path_key: str, title: str) -> list[ParsedSource]:
    """One ParsedSource per conversation, so each is an attributable unit."""
    sources = []
    for conv in data:
        if not isinstance(conv, dict):
            continue
        cid = conv.get("id") or conv.get("conversation_id") or conv.get("title") or "conversation"
        ctitle = (conv.get("title") or "Untitled conversation").strip()
        doc = EntityRef(type="conversation", name=ctitle, key=normalize(f"{path_key}#{cid}"), method="source",
                        attrs={"platform": "chatgpt", "conversation_id": cid})
        chunks = []
        for m in _chatgpt_messages(conv):
            for i, (_, _, piece) in enumerate(split_text(m["text"])):
                chunks.append(Chunk(locator=f"conv:{cid}/msg:{m['id']}" + (f"#{i + 1}" if i else ""),
                                    text=piece, title=ctitle, ts=m["ts"], meta={"role": m["role"]}))
        if chunks:
            sources.append(ParsedSource(kind="chatgpt", title=ctitle, doc=doc, chunks=chunks,
                                        ts=iso_from_epoch(conv.get("create_time")),
                                        meta={"conversation_id": cid}))
    return sources


def parse_json(path: Path, path_key: str) -> list[ParsedSource]:
    if path.stat().st_size > MAX_ZIP_MEMBER_BYTES:
        raise SkipSource("JSON too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SkipSource(f"not valid JSON: {exc}") from exc
    if not _looks_like_chatgpt(data):
        raise SkipSource("JSON is not a ChatGPT conversations export")
    return parse_chatgpt_data(data, path_key, path.stem)


def parse_zip(path: Path, path_key: str) -> list[ParsedSource]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SkipSource("not a valid zip") from exc
    with zf:
        member = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "conversations.json"), None)
        if not member:
            raise SkipSource("zip has no conversations.json (only ChatGPT export zips are ingested)")
        if zf.getinfo(member).file_size > MAX_ZIP_MEMBER_BYTES:
            raise SkipSource("conversations.json too large")
        data = json.loads(zf.read(member).decode("utf-8", errors="replace"))
    if not _looks_like_chatgpt(data):
        raise SkipSource("conversations.json is not in ChatGPT export format")
    return parse_chatgpt_data(data, path_key, path.stem)


# --------------------------------------------------------------------------- git history

def git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def parse_git_log(repo: Path, path_key: str, limit: int = GIT_LOG_LIMIT) -> ParsedSource:
    sep, end = "\x1f", "\x1e"
    fmt = sep.join(["%H", "%an", "%ae", "%aI", "%s", "%b"]) + end
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", f"-n{limit}", f"--format={fmt}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SkipSource(f"git unavailable: {exc}") from exc
    if out.returncode != 0:
        raise SkipSource(f"git log failed: {out.stderr.strip()[:200]}")
    name = repo.name
    doc = EntityRef(type="repository", name=name, key=normalize(path_key), method="source",
                    attrs={"path": str(repo)})
    chunks = []
    for record in out.stdout.split(end):
        fields = record.strip("\n").split(sep)
        if len(fields) < 5:
            continue
        sha, an, ae, date, subject = fields[:5]
        body = fields[5].strip() if len(fields) > 5 else ""
        person = _person(an, ae.lower()) if "@" in ae else EntityRef(type="person", name=an, method="git")
        person.method = "git_author"
        rel = RelationRef(doc, "has_contributor", person, evidence=f"commit:{sha[:12]}", method="git_author")
        text = f"Commit {sha[:12]} by {an} on {date}\n{subject}" + (f"\n\n{body}" if body else "")
        chunks.append(Chunk(locator=f"commit:{sha}", text=text[:MAX_CHUNK_CHARS], title=subject[:200], ts=date,
                            entities=[person], relations=[rel]))
    if not chunks:
        raise SkipSource("repository has no commits")
    return ParsedSource(kind="git", title=name, doc=doc, chunks=chunks, meta={"commits": len(chunks)})


# --------------------------------------------------------------------------- dispatch

def parse_file(path: Path, path_key: str) -> list[ParsedSource]:
    ext = path.suffix.lower()
    if ext in MARKDOWN_EXTS:
        return [parse_markdown(path, path_key)]
    if ext in TEXT_EXTS:
        return [parse_text(path, path_key)]
    if ext == ".pdf":
        return [parse_pdf(path, path_key)]
    if ext == ".docx":
        return [parse_docx(path, path_key)]
    if ext == ".xlsx":
        return [parse_xlsx(path, path_key)]
    if ext == ".eml":
        return [parse_eml(path, path_key)]
    if ext == ".mbox":
        return [parse_mbox(path, path_key)]
    if ext == ".json":
        return parse_json(path, path_key)
    if ext == ".zip":
        return parse_zip(path, path_key)
    raise SkipSource(f"unsupported extension {ext}")
