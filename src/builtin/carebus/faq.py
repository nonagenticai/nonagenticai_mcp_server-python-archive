"""faq tool: mounted under namespace "faq" → faq_ask. Answers come from strata_rag POST /api/ask."""

from __future__ import annotations

import uuid

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .client import strata_rag

faq_mcp = FastMCP(name="carebus-faq")

# strata_rag MAX_QUERY_STORES: most stores one query may name in leaf_ids.
MAX_LEAF_IDS = 256


def _leaf_ids(value: object) -> list[str]:
    """Validate the store filter: 1..MAX_LEAF_IDS uuid strings, returned unchanged."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LEAF_IDS:
        raise ToolError(f"[permanent] leaf_ids must be a list of 1-{MAX_LEAF_IDS} store ids")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str):
            raise ToolError("[permanent] leaf_ids must be store uuids")
        try:
            uuid.UUID(v)
        except ValueError:
            raise ToolError("[permanent] leaf_ids must be store uuids") from None
        out.append(v)
    return out


@faq_mcp.tool()
async def ask(question: str, leaf_ids: list[str] | None = None) -> dict:
    """Answer a general question from the knowledge stores, with cited sources.

    leaf_ids (care-bus sets it, never the model): search exactly these leaf stores instead of routing
    from the tenant's root; care-bus passes the stores outside every legal-matter wall. The answer then
    carries scope.leaf_ids, the filter applied, which care-bus requires before it uses the answer.
    """
    body: dict = {"query": question, "route_mode": "hybrid", "top_k": 8}
    scope = None
    if leaf_ids is not None:
        scope = _leaf_ids(leaf_ids)
        body["leaf_ids"] = scope
    r = await strata_rag().post("/api/ask", body)
    citations = list(r.get("citations", []))  # chunk UUIDs (strata_rag AskResponse)
    by_chunk = {res.get("chunk_id"): res for res in r.get("results", [])}
    # title / source / pages identify the cited document for the reader ("Survey, p. 2-3");
    # strata_rag sends page_start / page_end as null when the document has no pages.
    sources = [
        {
            "chunk_id": cid,
            "document_id": by_chunk[cid].get("document_id"),
            "store_path": by_chunk[cid].get("store_path"),
            "title": by_chunk[cid].get("title"),
            "source": by_chunk[cid].get("source"),
            "page_start": by_chunk[cid].get("page_start"),
            "page_end": by_chunk[cid].get("page_end"),
        }
        for cid in citations
        if cid in by_chunk
    ]
    out = {
        "answer": r.get("answer", ""),
        "citations": citations,
        "sources": sources,
        "timings_ms": r.get("timings_ms", {}),
    }
    if scope is not None:
        out["scope"] = {"leaf_ids": scope}
    return out
