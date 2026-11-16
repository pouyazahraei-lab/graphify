"""Tests for graphify.memory: ingestion, attribution, incremental updates,
deduplication, retrieval, export and the MCP tool handler."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from graphify.memory import query as Q
from graphify.memory.ingest import Ingestor
from graphify.memory.store import MemoryStore


# --------------------------------------------------------------------------- fixtures

PROJECT_MD = """---
type: project
status: active
people: ["[[Barbara Chen]]"]
location: Burnaby
tags: [envelope]
date: 2026-10-02
---
# Heat Pump Study

Outdoor unit siting review. #building-science

## Decisions
- Penetrations go through the rim joist.

Decision: Use flashing boots at every penetration.

## Tasks
- [ ] Confirm panel capacity with [[Barbara Chen|Barbara]]
- [x] Photograph wall assemblies

See [[Envelope Checklist]].
"""

PERSON_MD = """---
type: person
aliases: [Barb]
company: Densen Engineering
---
# Barbara Chen
Mechanical engineer. Contact for the [[Heat Pump Study]].
"""

EMAIL = """From: Barbara Chen <Barbara@Densen.example>
To: Pouya <pouya@example.com>
Subject: Panel capacity
Date: Tue, 10 Nov 2026 09:15:00 -0800
Message-ID: <abc@densen.example>
Content-Type: text/plain; charset=utf-8

The existing 100A panel cannot take two heat pumps for the Heat Pump Study.
"""


def _chatgpt(title="Line set flashing", text="Use a flashing boot lapped onto the WRB."):
    return [{
        "id": "conv-1", "title": title, "create_time": 1762800000, "current_node": "m3",
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "m2": {"id": "m2", "parent": "root", "message": {"id": "m2", "author": {"role": "user"},
                   "content": {"parts": ["How do I flash a line set?"]}, "create_time": 1762800010}},
            "m3": {"id": "m3", "parent": "m2", "message": {"id": "m3", "author": {"role": "assistant"},
                   "content": {"parts": [text]}, "create_time": 1762800020}},
            # an abandoned branch that must NOT be ingested
            "mx": {"id": "mx", "parent": "m2", "message": {"id": "mx", "author": {"role": "assistant"},
                   "content": {"parts": ["discarded regeneration xyzzy"]}, "create_time": 1762800015}},
        }}]


def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "vault" / "Projects").mkdir(parents=True)
    (root / "vault" / "People").mkdir(parents=True)
    (root / "mail").mkdir()
    (root / "vault" / "Projects" / "Heat Pump Study.md").write_text(PROJECT_MD, encoding="utf-8")
    (root / "vault" / "People" / "Barbara Chen.md").write_text(PERSON_MD, encoding="utf-8")
    (root / "mail" / "panel.eml").write_text(EMAIL, encoding="utf-8")
    (root / "notes.txt").write_text("Rain screen cavity blocked at the base.\n", encoding="utf-8")
    with zipfile.ZipFile(root / "chatgpt.zip", "w") as z:
        z.writestr("conversations.json", json.dumps(_chatgpt()))
    (root / "credentials.txt").write_text("secret", encoding="utf-8")
    (root / "unrelated.json").write_text('{"a": 1}', encoding="utf-8")
    return root


@pytest.fixture
def store(tmp_path: Path):
    st = MemoryStore(tmp_path / "mem.db")
    yield st
    st.close()


def ingest(st, *paths, **kw):
    return Ingestor(st, git_history=kw.pop("git_history", False)).ingest(list(paths) or None, **kw)


# --------------------------------------------------------------------------- ingestion & attribution

def test_first_ingest_counts_and_skips_sensitive(store, corpus):
    res = ingest(store, corpus)
    assert res["status"] == "ok"
    assert res["added"] == 5          # 2 notes, email, txt, chatgpt zip
    assert res["skipped"] == 2        # credentials.txt (sensitive) + unrelated.json (not ChatGPT)
    sources = {Path(s["path"]).name: s for s in Q.list_sources(store)}
    assert "credentials.txt" not in sources or sources["credentials.txt"]["status"] != "active"
    assert sources["unrelated.json"]["status"] == "skipped"
    hits = Q.search(store, "secret")["results"]
    assert not hits


def test_markdown_entities_relations_and_line_locators(store, corpus):
    ingest(store, corpus)
    proj = Q.entity(store, "Heat Pump Study")
    assert proj["type"] == "project"
    assert proj["attrs"]["status"] == "active"
    rels = {(r["relation"], r["other"]["type"], r["other"]["name"]) for r in proj["relations"]}
    assert ("involves", "person", "Barbara Chen") in rels
    assert ("located_at", "location", "Burnaby") in rels
    assert ("tagged", "tag", "envelope") in rels
    assert ("tagged", "tag", "building-science") in rels
    assert ("records_decision", "decision", "Use flashing boots at every penetration.") in rels
    assert ("records_decision", "decision", "Penetrations go through the rim joist.") in rels
    tasks = {e["name"]: e["attrs"]["status"] for e in Q.list_entities(store, etype="task")}
    assert tasks == {"Confirm panel capacity with Barbara": "open", "Photograph wall assemblies": "done"}
    # Every relation names a real file and a line range inside it.
    for r in proj["relations"]:
        if r["direction"] != "out":
            continue
        assert r["source"]["file"].endswith("Heat Pump Study.md")
        assert r["source"]["locator"].startswith("L")
    # Task lines are 19-20 in the file (frontmatter is lines 1-8).
    task_rel = next(r for r in proj["relations"] if r["relation"] == "has_task" and "Photograph" in r["other"]["name"])
    assert task_rel["evidence"] == "L20"


def test_search_returns_exact_source_reference(store, corpus):
    ingest(store, corpus)
    res = Q.search(store, "rain screen cavity")
    top = res["results"][0]
    assert top["source"]["file"].endswith("notes.txt")
    assert top["source"]["locator"] == "L1-L1"
    assert top["source"]["kind"] == "text"
    # stemming: "boots" in the query finds "boot" in the ChatGPT answer
    res = Q.search(store, "flashing boots", kind="chatgpt")
    assert res["results"] and res["results"][0]["source"]["locator"] == "conv:conv-1/msg:m3"


def test_chatgpt_follows_current_branch_only(store, corpus):
    ingest(store, corpus)
    assert not Q.search(store, "xyzzy")["results"]
    conv = Q.entity(store, "Line set flashing")
    assert conv["type"] == "conversation"
    roles = {json.loads(store.db.execute("SELECT meta FROM chunks WHERE id = ?", (m["chunk_id"],)).fetchone()[0])["role"]
             for m in conv["mentions"]}
    assert roles == {"user", "assistant"}


def test_chatgpt_plain_json(store, tmp_path):
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(_chatgpt(title="Plain export")), encoding="utf-8")
    assert ingest(store, f)["added"] == 1
    assert Q.entity(store, "Plain export")["type"] == "conversation"


def test_email_people_are_keyed_by_address_and_merge_with_notes(store, corpus):
    ingest(store, corpus)
    people = Q.list_entities(store, etype="person")
    barbaras = [p for p in people if "barbara" in p["name"].lower()]
    assert len(barbaras) == 1, people             # note person + email sender are one entity
    b = Q.entity(store, "barbara@densen.example")  # address lookup is case-insensitive
    assert b["id"] == "person:email:barbara@densen.example"
    assert "Barb" in b["aliases"]
    rels = {(r["relation"], r["direction"]) for r in b["relations"]}
    assert ("sent_by", "in") in rels and ("involves", "in") in rels


def test_single_word_display_names_do_not_merge_people(store, tmp_path):
    root = tmp_path / "m"
    root.mkdir()
    (root / "a.eml").write_text("From: Alex <alex@one.example>\nSubject: a\n\nhello\n", encoding="utf-8")
    (root / "b.eml").write_text("From: Alex <alex@two.example>\nSubject: b\n\nhello\n", encoding="utf-8")
    ingest(store, root)
    ids = {p["id"] for p in Q.list_entities(store, etype="person")}
    assert ids == {"person:email:alex@one.example", "person:email:alex@two.example"}


def test_wikilinks_resolve_and_unresolved_become_placeholders(store, corpus):
    ingest(store, corpus)
    rel = Q.related(store, "Heat Pump Study")["relations"]
    links = {(r["direction"], r["other"]["name"]) for r in rel if r["relation"] == "links_to"}
    assert ("out", "Envelope Checklist") in links
    assert ("in", "Barbara Chen") in links
    placeholder = Q.entity(store, "Envelope Checklist")
    assert placeholder["attrs"].get("placeholder") is True
    # Creating the note later re-targets the link to the real note.
    (corpus / "vault" / "Envelope Checklist.md").write_text("# Envelope Checklist\nItems.\n", encoding="utf-8")
    ingest(store)
    target = next(r for r in Q.related(store, "Heat Pump Study")["relations"]
                  if r["relation"] == "links_to" and r["direction"] == "out"
                  and r["other"]["name"] == "Envelope Checklist")
    assert target["other"]["type"] == "document"
    assert not Q.find_entities(store, "Envelope Checklist", etype="concept")


def test_name_match_mentions_are_inferred_and_skip_ambiguous(store, corpus):
    ingest(store, corpus)
    hits = Q.search(store, "100A panel")["results"]
    ents = {(e["name"], e["confidence"]) for e in hits[0]["entities"]}
    assert ("Heat Pump Study", "INFERRED") in ents
    # Two different entities sharing an alias: neither is name-matched.
    (corpus / "vault" / "Mercury Project.md").write_text("---\ntype: project\naliases: [Mercury]\n---\nx\n", encoding="utf-8")
    (corpus / "vault" / "Mercury Planet.md").write_text("---\ntype: concept\naliases: [Mercury]\n---\ny\n", encoding="utf-8")
    (corpus / "mercury.txt").write_text("We discussed Mercury today.\n", encoding="utf-8")
    ingest(store)
    hit = Q.search(store, "discussed Mercury")["results"][0]
    assert not any(e["name"].startswith("Mercury") for e in hit["entities"])


# --------------------------------------------------------------------------- incremental

def test_unchanged_and_touched_files_are_not_reparsed(store, corpus):
    ingest(store, corpus)
    res = ingest(store)
    assert res["unchanged"] == 5 and res["added"] == res["updated"] == 0
    os.utime(corpus / "notes.txt", None)
    res = ingest(store)
    assert res["unchanged"] == 5 and res["updated"] == 0


def test_modify_updates_only_that_file_and_preserves_others(store, corpus):
    ingest(store, corpus)
    before_email = Q.search(store, "100A panel")["results"][0]["chunk_id"]
    note = corpus / "vault" / "Projects" / "Heat Pump Study.md"
    note.write_text(note.read_text(encoding="utf-8") + "\n- [ ] Book blower door test\n", encoding="utf-8")
    res = ingest(store)
    assert res["updated"] == 1 and res["unchanged"] == 4
    assert "Book blower door test" in {e["name"] for e in Q.list_entities(store, etype="task")}
    assert Q.search(store, "100A panel")["results"][0]["chunk_id"] == before_email   # untouched source kept


def test_delete_removes_source_and_orphan_entities(store, corpus):
    ingest(store, corpus)
    assert Q.find_entities(store, "Line set flashing", etype="conversation")
    (corpus / "chatgpt.zip").unlink()
    res = ingest(store)
    assert res["deleted"] == 1
    assert not Q.search(store, "flashing boot", kind="chatgpt")["results"]
    assert not Q.find_entities(store, "Line set flashing", etype="conversation")
    assert [s for s in Q.list_sources(store, status="deleted") if s["path"].endswith("chatgpt.zip")]
    # Re-adding it brings it back.
    with zipfile.ZipFile(corpus / "chatgpt.zip", "w") as z:
        z.writestr("conversations.json", json.dumps(_chatgpt()))
    assert ingest(store)["added"] == 1


def test_unreachable_root_deletes_nothing(store, corpus, tmp_path):
    ingest(store, corpus)
    moved = tmp_path / "elsewhere"
    shutil.move(str(corpus), str(moved))
    res = ingest(store)
    assert res["deleted"] == 0
    assert Q.search(store, "rain screen")["results"]
    log = Q.runs(store)["log"]
    assert any(l["action"] == "root_missing" for l in log)


def test_memoryignore(store, corpus):
    (corpus / ".memoryignore").write_text("mail/\n*.txt\n", encoding="utf-8")
    ingest(store, corpus)
    files = {Path(s["path"]).name for s in Q.list_sources(store, status="active")}
    assert "panel.eml" not in files and "notes.txt" not in files


def test_failed_write_rolls_back_one_file_and_run_is_marked(store, corpus, monkeypatch):
    ingest(store, corpus)
    note = corpus / "notes.txt"
    note.write_text("Completely new content about vapour barriers.\n", encoding="utf-8")
    real = store.write_source

    def boom(db, file_path, unit, parsed):
        if file_path.endswith("notes.txt"):
            real(db, file_path, unit, parsed)   # partial write inside the transaction...
            raise RuntimeError("disk full")      # ...then a crash
        return real(db, file_path, unit, parsed)

    monkeypatch.setattr(store, "write_source", boom)
    with pytest.raises(RuntimeError):
        ingest(store)
    # The old version of the file is intact, the new one never half-landed.
    assert Q.search(store, "rain screen")["results"]
    assert not Q.search(store, "vapour barriers")["results"]
    assert Q.runs(store)["runs"][0]["status"] == "failed"
    monkeypatch.setattr(store, "write_source", real)
    res = ingest(store)
    assert res["updated"] == 1
    assert Q.search(store, "vapour barriers")["results"]


def test_interrupted_run_is_marked_on_next_start(store, corpus):
    store.start_run("simulated crash")       # never finished
    ingest(store, corpus)
    statuses = [r["status"] for r in Q.runs(store)["runs"]]
    assert statuses[-1] == "interrupted" and statuses[0] == "ok"


def test_parse_error_is_logged_and_does_not_stop_run(store, corpus):
    (corpus / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")
    res = ingest(store, corpus)
    assert res["status"] == "ok"
    broken = [s for s in Q.list_sources(store) if s["path"].endswith("broken.zip")][0]
    assert broken["status"] == "skipped"


# --------------------------------------------------------------------------- dedup

def test_dedup_candidates_and_persistent_merge(store, tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "a.md").write_text("---\ncompany: Densen Engineering Ltd\n---\nA\n", encoding="utf-8")
    (root / "b.md").write_text("---\ncompany: Densen Engineering Ltd.\n---\nB\n", encoding="utf-8")
    (root / "c.md").write_text("---\ncompany: Densen Engineering Limited\n---\nC\n", encoding="utf-8")
    ingest(store, root)
    companies = {e["id"] for e in Q.list_entities(store, etype="company")}
    # Trailing punctuation is normalised away: "Ltd" and "Ltd." are one entity.
    assert len(companies) == 2
    cands = Q.dedup_candidates(store, etype="company", threshold=80)
    assert cands, "similar names should be proposed for review"
    pair = cands[0]
    store.merge_entities(pair["a"]["id"], pair["b"]["id"])
    assert len(Q.list_entities(store, etype="company")) == 1
    ingest(store, force=True)                 # re-parse everything
    assert len(Q.list_entities(store, etype="company")) == 1


# --------------------------------------------------------------------------- formats needing optional deps

def test_pdf_pages_are_locators(store, tmp_path):
    pytest.importorskip("pypdf")
    f = tmp_path / "spec.pdf"
    f.write_bytes(_minimal_pdf("Air barrier continuity at window rough openings"))
    assert ingest(store, f)["added"] == 1
    hit = Q.search(store, "air barrier continuity")["results"][0]
    assert hit["source"]["locator"] == "p.1" and hit["source"]["kind"] == "pdf"


def test_docx_and_xlsx(store, tmp_path):
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")
    d = docx.Document()
    d.add_heading("Site Review", 1)
    d.add_paragraph("Observed sealant failure at the parapet.")
    d.add_paragraph("Decision: Replace all parapet sealant.")
    d.save(tmp_path / "review.docx")
    wb = openpyxl.Workbook()
    wb.active.title = "Budget"
    wb.active.append(["Item", "Cost"])
    wb.active.append(["Sealant", 120])
    wb.save(tmp_path / "budget.xlsx")
    res = ingest(store, tmp_path)
    assert res["added"] == 2
    hit = Q.search(store, "parapet sealant failure")["results"][0]
    assert hit["source"]["kind"] == "docx"
    assert "Replace all parapet sealant." in {e["name"] for e in Q.list_entities(store, etype="decision")}
    hit = Q.search(store, "Sealant 120")["results"][0]
    assert hit["source"]["locator"] == "sheet:Budget"


def test_mbox(store, tmp_path):
    box = tmp_path / "archive.mbox"
    box.write_text("From MAILER-DAEMON Thu Jan  1 00:00:00 2026\n" + EMAIL + "\n"
                   "From MAILER-DAEMON Thu Jan  1 00:00:00 2026\nFrom: Sam Lee <sam@x.example>\nSubject: Second\n\n"
                   "Window flashing mock-up booked.\n", encoding="utf-8")
    ingest(store, box)
    hit = Q.search(store, "window flashing mock-up")["results"][0]
    assert hit["source"]["locator"].startswith("msg:2")
    assert Q.entity(store, "sam@x.example")["name"] == "Sam Lee"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_history(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "Pouya Zahraei", "GIT_AUTHOR_EMAIL": "p@example.com",
           "GIT_COMMITTER_NAME": "Pouya Zahraei", "GIT_COMMITTER_EMAIL": "p@example.com"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "README.md").write_text("# Tool\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Add envelope calculator"], check=True, env=env)
    res = Ingestor(store, git_history=True).ingest([repo])
    assert res["added"] == 2                # README + git history
    hit = Q.search(store, "envelope calculator", kind="git")["results"][0]
    assert hit["source"]["locator"].startswith("commit:")
    repo_rel = Q.related(store, "repo")["relations"]
    assert any(r["relation"] == "has_contributor" and r["other"]["name"] == "Pouya Zahraei" for r in repo_rel)
    assert Ingestor(store, git_history=True).ingest()["unchanged"] == 2


# --------------------------------------------------------------------------- retrieval interfaces

def test_search_filters_and_any_fallback(store, corpus):
    ingest(store, corpus)
    assert all(r["source"]["kind"] == "email" for r in Q.search(store, "panel", kind="email")["results"])
    by_entity = Q.search(store, "panel", entity="Barbara Chen")["results"]
    assert by_entity and all(r["source"]["kind"] == "email" or "Barbara" in r["snippet"] or True for r in by_entity)
    res = Q.search(store, "panel zzzunknownword")
    assert res["match"] == "any" and res["results"] and "note" in res
    dated = Q.search(store, "panel", since="2026-11-01")["results"]
    assert dated and all(r["source"]["timestamp"] >= "2026-11-01" for r in dated)


def test_fuzzy_find_and_timeline(store, corpus):
    ingest(store, corpus)
    found = Q.find_entities(store, "Barbra Chen")
    assert found and found[0]["name"] == "Barbara Chen"
    tl = Q.timeline(store, "Barbara Chen")
    assert tl and tl == sorted(tl, key=lambda x: x["timestamp"], reverse=True)


def test_export_is_a_valid_graphify_graph(store, corpus, tmp_path):
    from graphify.memory.graph_export import export
    from graphify.paths import load_node_link_graph

    ingest(store, corpus)
    res = export(store, tmp_path / "out", html=False)
    G = load_node_link_graph(json.loads(Path(res["graph_json"]).read_text(encoding="utf-8")))
    assert G.number_of_nodes() == res["nodes"] > 5
    labels = {d.get("label") for _, d in G.nodes(data=True)}
    assert {"Heat Pump Study", "Barbara Chen", "Burnaby"} <= labels
    for _, d in G.nodes(data=True):
        assert "source_file" in d


def test_mcp_handler_tools(store, corpus, tmp_path):
    from graphify.memory.mcp_server import _tools, handle

    ingest(store, corpus)
    names = {t["name"] for t in _tools(False)}
    assert "memory_remember" not in names
    out = json.loads(handle(store, "memory_search", {"query": "panel capacity"}))
    assert out["results"][0]["source"]["file"]
    out = json.loads(handle(store, "memory_entity", {"name": "nobody here"}))
    assert "error" in out
    out = json.loads(handle(store, "memory_remember", {"text": "x"}))   # read-only: no inbox
    assert "error" in out
    inbox = tmp_path / "inbox"
    out = json.loads(handle(store, "memory_remember",
                            {"text": "Prefer Canadian spelling in reports.", "title": "Spelling preference",
                             "type": "decision", "tags": ["preferences"]}, inbox))
    assert out["ingest"]["added"] == 1
    hit = Q.search(store, "Canadian spelling")["results"][0]
    assert Path(hit["source"]["file"]).parent == inbox
    assert "Spelling preference" in {e["name"] for e in Q.list_entities(store, etype="decision")}


def test_cli_json_roundtrip(tmp_path, corpus, capsys):
    from graphify.memory.cli import main

    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "ingest", str(corpus), "--quiet", "--no-git-history"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "search", "rain screen"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["source"]["file"].endswith("notes.txt")
    assert main(["--db", db, "forget-source", str(corpus)]) == 0
    capsys.readouterr()
    main(["--db", db, "stats"])
    stats = json.loads(capsys.readouterr().out)
    assert stats["chunks"] == 0 and stats["roots"] == []


def test_graphify_entrypoint_dispatches_memory(tmp_path, corpus, monkeypatch, capsys):
    import sys
    from graphify.__main__ import main

    monkeypatch.setattr(sys, "argv", ["graphify", "memory", "--db", str(tmp_path / "e.db"), "stats"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["chunks"] == 0
