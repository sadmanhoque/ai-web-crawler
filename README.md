# LocalMind crawler + indexer + partner tiering

```
 sites.yaml ───────┐                        ┌────────────── Qdrant ──────────────┐
 PATCH /sites/...  ├─► crawler/planner ──► Redis Stream ──► indexer (N replicas) ─► collections
 POST /hits ───────┘   (Postgres registry,  index:events    chunk + embed + upsert   docs / encyclopedia / news / <site>
 partner feeds ──────► feed watcher (crawl/rss/api) ─► ingest gate ──────────────┘
                          unchanged/boilerplate/thin -> skip, old -> t3 vault
 tier bot (scheduled, heat = decayed views + impressions)
   promote ─► re-embed at the target tier's chunk depth
   demote  ─► archive to page_archives FIRST, then delete points (metadata.source_url)
 search app ─► GET /search (keyword leg over page_stats + page_archives),
               POST /restore, GET /web (Google CSE / Bing union source)
```

## Concepts
- **Profile** (config/profiles.yaml): a category with its embedding model, vector size, chunk size/overlap
  and default collection. Sites in the same profile share that collection.
- **Site**: belongs to a profile. `collection_override` gives it its own collection (still using the profile's
  embedding config). Every point carries `metadata.site_id` / `metadata.domain` (payload-indexed), so a site
  can be filtered, deleted or moved at any time.
- **Depth**: `headline` (title+h1+description) / `summary` (LLM, cached by content hash) / `full`.
  Precedence: path_rules > depth_override > auto (visit_score from `/hits`; hot pages promoted a tier).
- **Schedule**: each site has `interval_hours` (24) and a UTC slot (`run_hour_utc:run_minute_utc`). New sites are
  assigned the least-loaded hour so crawls are staggered. `next_run_at` is stored in Postgres.
- **Partner** (Postgres `partners`): a contracted content site with `profile`, `feed_type`
  (`crawl` / `rss` / `api`), optional per-partner tiering overrides + its own Qdrant collection.
  Onboarding is one row in `partners` (inserted by your own tooling — there is no API or script for it);
  the feed watcher picks new rows up on its next tick and auto-creates the `sites` row, which starts
  the crawl and the tiering lifecycle.
- **Tiers** (S3-style storage classes, `docs/tiering.md`): lower tier = less findable by vector search.
  `t0` full -> `t1` lede -> `t2` one stub -> `t3` vault (no vectors; Postgres metadata + archived body,
  findable by keyword/title only). Pages enter at `t0` and move one step per evaluation, decided by **heat**:
  `(hits × view_weight + impressions × impression_weight) × 0.5^(idle_days / half_life_days)`, with hysteresis
  floors (demote below, promote above) and a min-dwell anti-flapping gate. `config/tiers.yaml` is the
  declarative source of truth.
- **Ingest gate** (`shared/gate.py`): unchanged hash -> boilerplate path -> thin (<60 words) -> age
  (older than `backfill_cutoff_days` -> straight to the vault) -> duplicate content. A 20-year archive costs
  metadata, not vectors.

## API (Authorization: Bearer $ADMIN_TOKEN)
```
GET   /plan                               what will be indexed: collection, depth, slot, next run
PUT   /sites/{domain}                     create/update  {"profile":"docs","depth_override":"full","run_hour_utc":19}
PATCH /sites/{domain}                     e.g. {"interval_hours":3}  (time-sensitive)  or {"run_hour_utc":6}
POST  /sites/{domain}/run-now             run at the next scheduler tick; normal schedule resumes after
POST  /sites/{domain}/move                {"collection":"wikipedia_en"}  or {"collection":null} to go back to shared
POST  /hits                               [{"url":..., "site_domain":..., "count":1, "impressions":0}]  from your search server
POST  /admin/sync-manual
```
`/hits` is the tiering signal: `count` = page opens (views), `impressions` = times served in a result set or an
AI answer (AI-cited visits count). A first hit on a vaulted page re-creates its `page_stats` row **at t3**, not
the t0 default, so heat promotes it back up instead of the bot demoting a hot page.

`sites.yaml` is re-applied on startup and wins for the fields it lists; use PATCH for runtime changes to
sites not pinned in the yaml, or edit the yaml + `/admin/sync-manual`.

## Search app (no auth)
The tiering-related slice of the search server (`search/app.py`); the full union pipeline (Qdrant fan-out +
cross-encoder rerank) lands in this service later. Run with the same `DATABASE_URL`/`REDIS_URL` as crawler+indexer.
```
GET  /search?q=&partner=&limit=    keyword leg: indexed (t0-t2) + vaulted (t3) pages matching
                                   title/URL/body; t3 results are labelled source "vault"
POST /restore {url, tier=t0|t1|t2} re-embed a vaulted page from page_archives (no live fetch)
GET  /web?q=&provider=&num=        web search leg (search/web.py): Google CSE or Bing, same
                                   hit shape. Provider via param or WEB_SEARCH_PROVIDER (default google);
                                   keys CSE_API_KEY+CSE_CX / BING_API_KEY. Missing key -> 400,
                                   upstream 403/429/5xx -> 502 (the union drops that leg, query still answers)
GET  /health
```

## tierctl (CLI monitoring/debug)
```
tierctl status [--partner D]      tier distribution + est. storage, per partner/site
tierctl heat URL                  hits/impressions/idle/decay, computed heat, current vs expected tier
tierctl dry-run [--partner D]     planned transitions (same rows the bot sees), applies nothing
tierctl promote/demote/restore    manual transition — the same pipeline as the bot (audited as via=manual)
tierctl transitions [--since]     the tier_events audit log (promote/demote/restore/archive, newest first)
tierctl metrics [--partner D]     pages/storage per tier, transition rate, restore latency, vault depth
tierctl config [D]                effective tiering params, per-parameter provenance (file/DB drift)
```

## Testing & verification
- `tests/` — unit tests for the pure decision modules and the stateful paths: tiers, gate, extract,
  tierbot, keyword, backfill, metrics, web (stubbed HTTP, no keys needed).
- `docs/smoke-testing.md` — the end-to-end checklist: a fixture partner (`scripts/smoke_server.py`,
  7 pages with controlled dates covering every ingest-gate outcome), traffic simulation through `/hits`,
  and the full demote -> vault -> restore cycle on a fast-forward config (`config/smoke-fast.yaml`,
  ~29-min half-life / ~2.5-min dwell) so the lifecycle runs in minutes instead of weeks. Section 9 walks
  the live CSE/Bing leg.

## Behaviours worth knowing
- Re-indexing is idempotent (deterministic point ids). After a *complete* crawl the indexer deletes that site's
  points from older crawls (vanished pages, depth changes). Truncated/erroring crawls never delete.
- Move copies vectors as-is (no re-embedding) then deletes from the source; the profile's embedding model must
  match the destination collection (checked).
- Payload layout is `{"page_content", "metadata": {...}}` (LangChain-compatible). `aws_service` was dropped.
- robots.txt is respected. Sites are crawled with a page cap (`max_pages`, default 200).
- Tiering is S3-style (t0 full -> t3 vault) by visit heat: demote archives to `page_archives` **before**
  deleting vectors (crash in between = page both archived and indexed, harmless — the next pass re-demotes).
  A failed archive fetch skips the demote (page stays warm — safe default). Full design: `docs/tiering.md`.
- Every applied tier action writes exactly one `tier_events` row (append-only audit log); holds, no-ops and
  failed fetches write nothing.

## Run
Shared-infra prerequisites (all in the `localmind` namespace, deployed by the
ai-web-search repo): the `localmind-pg` CNPG cluster is Ready, the
`localmind-crawler-pg` Secret is applied, and the `pg-crawler-db-init` Job has
completed — that Job creates role + database `localmind_crawler` in the shared
cluster, which is the crawler's only Postgres footprint:
    kubectl -n localmind get job pg-crawler-db-init

```
docker build -f crawler/Dockerfile -t localmind-crawler .
docker build -f indexer/Dockerfile -t localmind-indexer .
# see k8s/configmap.md, then:
kubectl apply -f k8s/crawler.yaml -f k8s/indexer.yaml
```

Search app (no Dockerfile yet — run it anywhere with the same credentials):

```
uvicorn search.app:app        # needs DATABASE_URL, REDIS_URL; TIERS_FILE for tierctl-style reads
```
