import os
from qdrant_client import QdrantClient, models
from shared.config import Profile

client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                      api_key=os.getenv("QDRANT_API_KEY") or None, prefer_grpc=False)
_known: set[str] = set()


def site_filter(site_id: int, *, not_crawl_id: str | None = None) -> models.Filter:
    must = [models.FieldCondition(key="metadata.site_id", match=models.MatchValue(value=site_id))]
    must_not = ([models.FieldCondition(key="metadata.crawl_id", match=models.MatchValue(value=not_crawl_id))]
                if not_crawl_id else None)
    return models.Filter(must=must, must_not=must_not)


def ensure_collection(name: str, prof: Profile):
    if name in _known:
        return
    if not client.collection_exists(name):
        try:
            client.create_collection(
                name,
                vectors_config=models.VectorParams(size=prof.vector_size, distance=models.Distance.COSINE),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
                optimizers_config=models.OptimizersConfigDiff(indexing_threshold=20_000))
            for key, schema in (("metadata.site_id", models.PayloadSchemaType.INTEGER),
                                ("metadata.crawl_id", models.PayloadSchemaType.KEYWORD),
                                ("metadata.domain", models.PayloadSchemaType.KEYWORD),
                                ("metadata.source_url", models.PayloadSchemaType.KEYWORD)):
                client.create_payload_index(name, key, schema)
        except Exception:
            if not client.collection_exists(name):      # lost a creation race with another replica? fine
                raise
    else:
        size = client.get_collection(name).config.params.vectors.size
        if size != prof.vector_size:
            raise ValueError(f"Collection '{name}' has dim {size}, profile '{prof.name}' needs {prof.vector_size}")
        pts, _ = client.scroll(name, limit=1, with_payload=True)
        existing = pts[0].payload.get("metadata", {}).get("embedding_model") if pts else None
        if existing and existing != prof.embedding_model:
            raise ValueError(f"Collection '{name}' holds '{existing}' vectors, profile wants '{prof.embedding_model}'")
        try:                                          # collections created before the source_url index
            client.create_payload_index(name, "metadata.source_url", models.PayloadSchemaType.KEYWORD)
        except Exception:
            pass                                      # index already exists
    _known.add(name)


def move_site(site_id: int, prof: Profile, src: str, dst: str) -> int:
    """Copy all of a site's points (vectors included, no re-embedding) src -> dst, then delete from src."""
    ensure_collection(dst, prof)
    flt, offset, moved = site_filter(site_id), None, 0
    while True:
        pts, offset = client.scroll(src, scroll_filter=flt, limit=256, offset=offset,
                                    with_payload=True, with_vectors=True)
        if pts:
            client.upsert(dst, points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in pts])
            moved += len(pts)
        if offset is None:
            break
    client.delete(src, points_selector=models.FilterSelector(filter=flt))
    return moved


def delete_url_points(collection: str, url: str) -> int:
    """Delete every point of one page (all tiers); returns how many were removed."""
    flt = models.Filter(must=[models.FieldCondition(key="metadata.source_url",
                                                    match=models.MatchValue(value=url))])
    pts, _ = client.scroll(collection, scroll_filter=flt, limit=500,
                           with_payload=False, with_vectors=False)
    if not pts:
        return 0
    client.delete(collection, models.FilterSelector(filter=flt))
    return len(pts)
