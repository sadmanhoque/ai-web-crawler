# LocalMind crawler + indexer

```
 Cloudflare Radar ─┐                       ┌────────────── Qdrant ──────────────┐
 sites.yaml ───────┼─► crawler/planner ──► Redis Stream ──► indexer (N replicas) ─► collections
 PATCH /sites/...  ┘   (Postgres registry,  index:events    chunk + embed + upsert   docs / encyclopedia / news / <site>
                        schedule, depth,
                        fetch, summarize)
```

## Concepts
- **Profile** (config/profiles.yaml): a category with its embedding model, vector size, chunk size/overlap
  and default collection. Sites in the same profile share that collection.
- **Site**: belongs to a profile. `collection_override` gives it its own collection (still using the profile's
  embedding config). Every point carries `metadata.site_id` / `metadata.domain` (payload-indexed), so a site
  can be filtered, deleted or moved at any time.
- **Depth**: `headline` (title+h1+description) / `summary` (LLM, cached by content hash) / `full`.
  Precedence: path_rules > depth_override > auto (visit_score from Radar rank and `/hits`; hot pages promoted a tier).
- **Schedule**: each site has `interval_hours` (24) and a UTC slot (`run_hour_utc:run_minute_utc`). New sites are
  assigned the least-loaded hour so crawls are staggered. `next_run_at` is stored in Postgres.

## API (Authorization: Bearer $ADMIN_TOKEN)
```
GET   /plan                               what will be indexed: collection, depth, slot, next run
PUT   /sites/{domain}                     create/update  {"profile":"docs","depth_override":"full","run_hour_utc":19}
PATCH /sites/{domain}                     e.g. {"interval_hours":3}  (time-sensitive)  or {"run_hour_utc":6}
POST  /sites/{domain}/run-now             run at the next scheduler tick; normal schedule resumes after
POST  /sites/{domain}/move                {"collection":"wikipedia_en"}  or {"collection":null} to go back to shared
POST  /hits                               [{"url":..., "site_domain":..., "count":1}]  from your search server
POST  /admin/sync-radar | /admin/sync-manual
```
`sites.yaml` is re-applied on startup and wins for the fields it lists; use PATCH for runtime changes to
sites not pinned in the yaml, or edit the yaml + `/admin/sync-manual`.

## Behaviours worth knowing
- Re-indexing is idempotent (deterministic point ids). After a *complete* crawl the indexer deletes that site's
  points from older crawls (vanished pages, depth changes). Truncated/erroring crawls never delete.
- Move copies vectors as-is (no re-embedding) then deletes from the source; the profile's embedding model must
  match the destination collection (checked).
- Payload layout is `{"page_content", "metadata": {...}}` (LangChain-compatible). `aws_service` was dropped.
- robots.txt is respected. Radar sites are crawled with a small page cap (default 200).

## Run
```
docker build -f crawler/Dockerfile -t localmind-crawler .
docker build -f indexer/Dockerfile -t localmind-indexer .
# see k8s/configmap.md, then:
kubectl apply -f k8s/crawler.yaml -f k8s/indexer.yaml
```
