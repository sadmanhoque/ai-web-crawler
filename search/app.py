"""Search side: keyword fallback + restore (build order 7, docs/tiering.md).

This is the tiering-related slice of the search server: t3 (vault) pages
have no vectors, so they stay findable through the keyword pass over
page_stats/page_archives, a surfaced vaulted page can be restored
(re-embedded from the archived copy) on demand, and the CSE/Bing web
search leg (search/web.py) is exposed for the union pipeline. The full
pipeline (Qdrant fan-out + cross-encoder rerank over all legs) lands in
this service later; these endpoints are what the tiering system requires
today.

Run (search side, same DATABASE_URL/REDIS_URL as crawler+indexer):
    uvicorn search.app:app
"""
from dataclasses import asdict

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from crawler.db import SessionLocal
from crawler.tierbot import restore_page
from shared.messages import get_redis
from shared.tiers import Tier

from .keyword import keyword_search
from .web import WebSearchError, web_search

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


@app.get("/web")
def web(q: str, provider: str | None = None, num: int = 10):
    """Web search leg (CSE/Bing union source, search/web.py): parallel
    retrieval alongside the vector + keyword legs. Provider: `provider`
    param, else WEB_SEARCH_PROVIDER (default google). Missing API key ->
    400 (misconfiguration); upstream 403/429/5xx -> 502 with the status."""
    try:
        hits = web_search(q, provider=provider, num=min(max(num, 1), 50))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except WebSearchError as e:
        raise HTTPException(502, str(e))
    return {"query": q, "provider": provider or os.getenv("WEB_SEARCH_PROVIDER", "google"),
            "results": [asdict(h) for h in hits]}
