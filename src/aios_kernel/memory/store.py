"""Memory & knowledge: global, namespace (organization / project) and agent-private tiers.

Vectors are stored next to their rows and compared in-process, which keeps the
default deployment to a single database. The search path is isolated behind
``MemoryService.search`` so a dedicated vector index (pgvector, Qdrant) can
replace the scan without touching callers.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np
from sqlalchemy import and_, or_, select

from ..db import Database, new_id
from ..errors import NotFound
from ..models import Document, MemoryItem

CHUNK_CHARS = 1400
CHUNK_OVERLAP = 180
_WORD = re.compile(r"[a-z0-9]{3,}")


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            # Prefer to cut at a paragraph or sentence boundary in the back half of the window.
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > CHUNK_CHARS // 2:
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return [c for c in chunks if c]


class MemoryService:
    def __init__(self, db: Database, namespaces, embedder):
        self.db = db
        self.namespaces = namespaces
        self.embedder = embedder

    async def add(self, *, namespace_id: str, content: str, title: str = "", kind: str = "note", scope: str = "namespace",
                  agent_id: str | None = None, document_id: str | None = None, meta: dict | None = None) -> list[MemoryItem]:
        chunks = chunk_text(content)
        if not chunks:
            return []
        vectors = await self.embedder.embed([f"{title}\n{c}" if title else c for c in chunks])
        items = [MemoryItem(id=new_id("mem"), namespace_id=namespace_id, scope=scope, agent_id=agent_id,
                            kind=kind, document_id=document_id, title=title, content=c, embedder=self.embedder.id,
                            embedding=vectors[i].astype(np.float32).tobytes(), meta={**(meta or {}), "chunk": i, "chunks": len(chunks)})
                 for i, c in enumerate(chunks)]
        async with self.db.session() as s:
            s.add_all(items)
        return items

    async def add_document(self, *, namespace_id: str, title: str, content: str, source: str = "", meta: dict | None = None) -> Document:
        doc = Document(id=new_id("doc"), namespace_id=namespace_id, title=title, source=source, content=content,
                       content_hash=hashlib.sha256(content.encode()).hexdigest(), meta=meta or {})
        async with self.db.session() as s:
            s.add(doc)
        await self.add(namespace_id=namespace_id, content=content, title=title, kind="document", document_id=doc.id)
        return doc

    async def documents(self, namespace_ids: list[str] | None = None) -> list[Document]:
        q = select(Document).order_by(Document.created_at.desc())
        if namespace_ids is not None:
            q = q.where(Document.namespace_id.in_(namespace_ids))
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def delete_document(self, document_id: str) -> None:
        async with self.db.session() as s:
            doc = await s.get(Document, document_id)
            if not doc:
                raise NotFound("document not found")
            for item in (await s.execute(select(MemoryItem).where(MemoryItem.document_id == document_id))).scalars():
                await s.delete(item)
            await s.delete(doc)

    async def search(self, query: str, *, namespace_id: str, agent_id: str | None = None, k: int = 6,
                     kinds: list[str] | None = None, strict_namespace: bool = False) -> list[dict]:
        """Semantic search over everything the caller is entitled to see.

        Visibility = the namespace itself, its ancestors (global and
        organization knowledge flow down), its descendants, and namespaces
        that granted it ``knowledge``. Agent-scoped items are private to
        ``agent_id``.
        """
        if strict_namespace:
            visible = {namespace_id}
        else:
            visible, _ = await self.namespaces.visible(namespace_id, "knowledge")
        private = and_(MemoryItem.scope == "agent", MemoryItem.agent_id == agent_id) if agent_id else None
        shared = MemoryItem.scope != "agent"
        q = select(MemoryItem).where(MemoryItem.namespace_id.in_(visible), MemoryItem.embedder == self.embedder.id,
                                     or_(shared, private) if private is not None else shared)
        if kinds:
            q = q.where(MemoryItem.kind.in_(kinds))
        q = q.order_by(MemoryItem.created_at.desc()).limit(20000)
        async with self.db.session() as s:
            items = list((await s.execute(q)).scalars())
        if not items:
            return []
        qv = (await self.embedder.embed([query]))[0]
        matrix = np.frombuffer(b"".join(i.embedding for i in items), dtype=np.float32).reshape(len(items), -1)
        sims = matrix @ qv
        # Light lexical boost: exact term overlap breaks ties between near-equal vectors.
        q_terms = set(_WORD.findall(query.lower()))
        if q_terms:
            for i, item in enumerate(items):
                overlap = len(q_terms & set(_WORD.findall((item.title + " " + item.content).lower())))
                sims[i] += 0.05 * overlap / len(q_terms)
        order = np.argsort(-sims)[:k]
        return [{"id": items[i].id, "score": round(float(sims[i]), 4), "title": items[i].title, "content": items[i].content,
                 "kind": items[i].kind, "scope": items[i].scope, "namespace_id": items[i].namespace_id,
                 "agent_id": items[i].agent_id, "created_at": items[i].created_at.isoformat() + "Z"}
                for i in order if sims[i] > 0.05]

    async def recent(self, namespace_ids: list[str] | None = None, limit: int = 100) -> list[MemoryItem]:
        q = select(MemoryItem).order_by(MemoryItem.created_at.desc()).limit(limit)
        if namespace_ids is not None:
            q = q.where(MemoryItem.namespace_id.in_(namespace_ids))
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars())

    async def delete(self, item_id: str) -> None:
        async with self.db.session() as s:
            item = await s.get(MemoryItem, item_id)
            if not item:
                raise NotFound("memory item not found")
            await s.delete(item)
