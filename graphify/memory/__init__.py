"""graphify.memory - local-first, persistent knowledge memory built on graphify.

A SQLite store (stdlib only) that ingests personal and professional sources
(Markdown/Obsidian, PDF, Word, Excel, plain text, email, ChatGPT exports, git
repositories), keeps per-chunk source attribution, extracts entities and
relationships with deterministic rules (no LLM, no network), updates
incrementally, and answers keyword / entity / relationship queries as JSON.

The store can be exported to a regular graphify ``graph.json`` so the existing
HTML, Obsidian and MCP graph tooling works on it unchanged.
"""
from __future__ import annotations

from graphify.memory.store import MemoryStore, default_db_path

__all__ = ["MemoryStore", "default_db_path"]
