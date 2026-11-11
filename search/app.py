"""Search side: keyword fallback + restore (build order 7, docs/tiering.md).

This is the tiering-related slice of the search server: t3 (vault) pages
have no vectors, so they stay findable through the keyword pass over
page_stats/page_archives, and a surfaced vaulted page can be restored
(re-embedded from the archived copy) on demand. The full pipeline (Qdrant
fan-out + CSE/Bing union + cross-encoder rerank) lands in this service
later; these endpoints are what the tiering system requires today.

Run (search side, same DATABASE_URL/REDIS_URL as crawler+indexer):
    uvicorn search.app:app
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from crawler.db import SessionLocal
from crawler.tierbot import restore_page
from shared.messages import get_redis
from shared.tiers import Tier

from .keyword import keyword_search

app = FastAPI(title="search (tiering slice)")


class RestoreReq(BaseModel):
    url: str
    tier: str = "t0"      # where to re-embed: t0 full (default), t1 lede, t2 stub


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/search")
def search(q: str, partner: str | None = None, limit: int = 20):
    """Keyword fallback leg: indexed (t0-t2) + vaulted (t3) pages that
    match by title/URL/body. The search server merges this with its vector
    hits before reranking."""
    return {"query": q, "results": keyword_search(q, partner=partner, limit=limit)}


@app.post("/restore")
def restore(body: RestoreReq):
    """Restore action: re-embed a vaulted page from page_archives (no live
    fetch). Publishes the PromoteMsg to the indexer stream and re-creates
    the page_stats row at the target tier; heat then re-tiers it as usual."""
    try:
        tier = Tier(body.tier)
    except ValueError:
        raise HTTPException(400, f"tier must be t0, t1 or t2 (got {body.tier!r})")
    if tier is Tier.t3:
        raise HTTPException(400, "restore target must be t0, t1 or t2")
    with SessionLocal() as db:
        try:
            result = restore_page(db=db, r=get_redis(), url=body.url, to_tier=tier)
        except LookupError as e:
            raise HTTPException(500, str(e))
    if result == "no_archive":
        raise HTTPException(404, f"no vaulted copy for {body.url}")
    return {"url": body.url, "result": result, "tier": tier.value}
