"""Indexer worker: consumes the Redis stream, chunks + embeds + upserts into Qdrant.
Scale by adding replicas (consumer group). No crawling happens here."""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import models
from shared.config import DEAD_STREAM, GROUP, STREAM, load_profiles
from shared.messages import (CloseMsg, DemoteMsg, MoveMsg, PageMsg, PromoteMsg,
                             get_redis, parse_event)
from shared.tiers import TIER_TO_DEPTH, Tier, tier_for_depth
from .qdrant_ops import client, delete_url_points, ensure_collection, move_site, site_filter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("indexer")
CONSUMER = f"indexer-{uuid.uuid4().hex[:8]}"
BATCH = 128
_models: dict = {}


def embed(model_name: str, texts: list[str]) -> list[list[float]]:
    if model_name not in _models:
        from sentence_transformers import SentenceTransformer
        _models[model_name] = SentenceTransformer(model_name)
    return _models[model_name].encode(texts, batch_size=32, normalize_embeddings=True,
                                      show_progress_bar=False).tolist()


def _index_sections(*, url, title, sections, site_id, domain, profile, collection,
                    tier, depth_label, now, crawl_id=None):
    prof = load_profiles()[profile]
    ensure_collection(collection, prof)
    splitter = RecursiveCharacterTextSplitter(chunk_size=prof.chunk_size,
                                              chunk_overlap=prof.chunk_overlap)
    texts, metas, ids = [], [], []
    for si, sec in enumerate(sections):
        for ci, chunk in enumerate(splitter.split_text(sec.text)):
            texts.append(chunk)
            ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#{si}#{ci}")))   # idempotent re-index
            md = {"source_url": url, "page_title": title, "section_heading": sec.heading,
                  "heading_tag": sec.heading_tag, "preview": sec.text[:150],
                  "site_id": site_id, "domain": domain, "profile": profile,
                  "depth": depth_label, "tier": tier.value,
                  "crawled_at": now, "embedding_model": prof.embedding_model}
            if crawl_id:
                md["crawl_id"] = crawl_id
            metas.append(md)
    for i in range(0, len(texts), BATCH):
        vecs = embed(prof.embedding_model, texts[i:i + BATCH])
        client.upsert(collection, points=[
            models.PointStruct(id=pid, vector=v, payload={"page_content": t, "metadata": md})
            for pid, v, t, md in zip(ids[i:i + BATCH], vecs, texts[i:i + BATCH], metas[i:i + BATCH])])


def handle_page(r, m: PageMsg):
    _index_sections(url=m.url, title=m.title, sections=m.sections, site_id=m.site_id,
                    domain=m.domain, profile=m.profile, collection=m.collection,
                    tier=tier_for_depth(m.depth.value), depth_label=m.depth.value,
                    now=datetime.now(timezone.utc).isoformat(), crawl_id=m.crawl_id)
    key = f"crawl:{m.crawl_id}"
    r.hincrby(key, "done", 1)
    r.expire(key, 7 * 86400)
    maybe_finalize(r, m.crawl_id)


def handle_promote(r, m: PromoteMsg):
    if m.to_tier is Tier.t3:                    # t3 has no depth; only demote goes there
        raise ValueError(f"invalid promote to t3 for {m.url}")
    delete_url_points(m.collection, m.url)      # drop old tier's points first
    _index_sections(url=m.url, title=m.title, sections=m.sections, site_id=m.site_id,
                    domain=m.domain, profile=m.profile, collection=m.collection,
                    tier=m.to_tier, depth_label=TIER_TO_DEPTH[m.to_tier],
                    now=datetime.now(timezone.utc).isoformat())
    log.info("promoted %s to %s (%d sections)", m.url, m.to_tier.value, len(m.sections))


def handle_demote(r, m: DemoteMsg):
    n = delete_url_points(m.collection, m.url)  # text already in page_archives
    log.info("demoted %s to %s, removed %d points", m.url, m.to_tier.value, n)


def handle_close(r, m: CloseMsg):
    key = f"crawl:{m.crawl_id}"
    r.hset(key, mapping={"total": m.total, "complete": int(m.complete), "site_id": m.site_id,
                         "collection": m.collection})
    r.expire(key, 7 * 86400)
    maybe_finalize(r, m.crawl_id)


def maybe_finalize(r, crawl_id: str):
    """Runs after every page and on close; fires once when all pages are indexed.
    Deletes points of this site from earlier crawls (pages that vanished or changed depth)."""
    key = f"crawl:{crawl_id}"
    h = r.hgetall(key)
    if "total" not in h or int(h.get("done", 0)) < int(h["total"]) or not r.hsetnx(key, "finalized", 1):
        return
    if h["complete"] == "1":
        client.delete(h["collection"], points_selector=models.FilterSelector(
            filter=site_filter(int(h["site_id"]), not_crawl_id=crawl_id)))
        log.info("crawl %s finalized, stale points removed", crawl_id)


def handle_move(r, m: MoveMsg):
    n = move_site(m.site_id, load_profiles()[m.profile], m.src, m.dst)
    log.info("moved %d points of site %d: %s -> %s", n, m.site_id, m.src, m.dst)


def process(r, mid: str, fields: dict):
    try:
        ev = parse_event(fields["data"])
        for attempt in range(3):
            try:
                {"page": handle_page, "close": handle_close, "move": handle_move,
                 "promote": handle_promote, "demote": handle_demote}[ev.type](r, ev)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    except Exception:
        log.exception("failed event %s -> dead-letter", mid)
        r.xadd(DEAD_STREAM, fields)
    finally:
        r.xack(STREAM, GROUP, mid)


def health_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    HTTPServer(("0.0.0.0", 8080), H).serve_forever()


def main():
    threading.Thread(target=health_server, daemon=True).start()
    r = get_redis()
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception:
        pass                                           # group exists
    for p in load_profiles().values():                 # warm models so the first message isn't slow
        embed(p.embedding_model, ["warmup"])
    log.info("indexer %s ready", CONSUMER)
    while True:
        claimed = r.xautoclaim(STREAM, GROUP, CONSUMER, min_idle_time=300_000, start_id="0-0", count=10)[1]
        for mid, fields in claimed:                    # take over work from crashed replicas
            if fields:
                process(r, mid, fields)
        for _, msgs in r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=16, block=5000) or []:
            for mid, fields in msgs:
                process(r, mid, fields)


if __name__ == "__main__":
    main()
