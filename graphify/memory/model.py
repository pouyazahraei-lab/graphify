"""Entity model, normalisation and identity rules for graphify.memory."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Entity types the memory layer understands. Anything else in a frontmatter
# ``type:`` field is stored as ``concept`` with the original value kept in attrs.
ENTITY_TYPES = (
    "person",
    "project",
    "company",
    "location",
    "document",
    "conversation",
    "repository",
    "concept",
    "decision",
    "event",
    "task",
    "tag",
)

# Frontmatter ``type:`` spellings mapped onto the canonical types.
TYPE_SYNONYMS = {
    "people": "person", "contact": "person", "human": "person",
    "projects": "project",
    "org": "company", "organisation": "company", "organization": "company",
    "client": "company", "business": "company", "firm": "company",
    "place": "location", "site": "location", "city": "location",
    "doc": "document", "note": "document", "reference": "document",
    "idea": "concept", "topic": "concept", "technology": "concept",
    "decisions": "decision", "adr": "decision",
    "meeting": "event", "milestone": "event",
    "todo": "task", "action": "task",
    "repo": "repository",
}

# Frontmatter keys whose values name other entities: key -> (type, relation).
FRONTMATTER_LINK_KEYS = {
    "person": ("person", "involves"),
    "people": ("person", "involves"),
    "attendees": ("person", "attended_by"),
    "participants": ("person", "involves"),
    "author": ("person", "authored_by"),
    "authors": ("person", "authored_by"),
    "owner": ("person", "owned_by"),
    "project": ("project", "part_of"),
    "projects": ("project", "part_of"),
    "company": ("company", "involves_org"),
    "companies": ("company", "involves_org"),
    "organization": ("company", "involves_org"),
    "organisation": ("company", "involves_org"),
    "client": ("company", "for_client"),
    "employer": ("company", "works_at"),
    "location": ("location", "located_at"),
    "place": ("location", "located_at"),
    "city": ("location", "located_at"),
    "topics": ("concept", "about"),
    "concepts": ("concept", "about"),
    "tags": ("tag", "tagged"),
}

_WS = re.compile(r"\s+")
_PUNCT_EDGE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_WIKILINK_TARGET = re.compile(r"^\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]$")


def normalize(name: str) -> str:
    """Identity key for an entity name: NFKC, casefolded, whitespace collapsed,
    edge punctuation stripped. Deliberately conservative: it never drops words
    or guesses abbreviations, so two different names never collapse silently."""
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    text = _WS.sub(" ", text).strip()
    text = _PUNCT_EDGE.sub("", text)
    return text


def clean_value(value: object) -> str:
    """A frontmatter value as display text: ``[[Alice]]`` -> ``Alice``."""
    text = str(value or "").strip().strip('"').strip("'").strip()
    m = _WIKILINK_TARGET.match(text)
    if m:
        text = m.group(1).strip()
    if text.startswith("#"):
        text = text[1:]
    return text


def canonical_type(raw: str | None) -> str | None:
    if not raw:
        return None
    t = normalize(raw)
    if t in ENTITY_TYPES:
        return t
    return TYPE_SYNONYMS.get(t)


def entity_id(etype: str, key: str) -> str:
    return f"{etype}:{key}"


@dataclass
class EntityRef:
    """An entity as found in one chunk, before resolution against the store."""

    type: str
    name: str
    key: str | None = None          # identity key; defaults to normalize(name)
    aliases: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    method: str = "rule"
    confidence: str = "EXTRACTED"

    def identity(self) -> str:
        return entity_id(self.type, self.key or normalize(self.name))


@dataclass
class RelationRef:
    src: EntityRef
    rel: str
    dst: EntityRef
    evidence: str = ""
    method: str = "rule"
    confidence: str = "EXTRACTED"


@dataclass
class Chunk:
    """A retrievable unit of text with its exact place in the original source."""

    locator: str                     # "L10-L42", "p.3", "msg:<id>", "sheet:Budget"
    text: str
    title: str = ""
    ts: str | None = None            # ISO-8601 timestamp if the source has one
    meta: dict = field(default_factory=dict)
    entities: list[EntityRef] = field(default_factory=list)
    relations: list[RelationRef] = field(default_factory=list)
    links: list[str] = field(default_factory=list)   # wikilink targets


@dataclass
class ParsedSource:
    """What an adapter returns for one source file."""

    kind: str
    title: str
    doc: EntityRef                   # the entity standing for the source itself
    chunks: list[Chunk]
    ts: str | None = None
    meta: dict = field(default_factory=dict)
