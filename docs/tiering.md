# Partner content tiering

S3-storage-class-style lifecycle for partner content in Qdrant. Partner sites
(`partners` table) are the only sites we index and the only sites we pay out
for. Goal: store hot content fully so vector search finds it, park cold
content where it costs ~10x less, without losing it.

Status: scaffold in place (build order 1-2 done). See checkboxes below.

## Tiers

| tier | name    | stored                                              | findable by           | ~cost/page |
|------|---------|-----------------------------------------------------|-----------------------|------------|
| t0   | full    | all chunk vectors + chunk text (Qdrant payload)     | vector + keyword      | ~9 KB      |
| t1   | summary | summary/lede chunk vectors + summary text           | vector + keyword      | ~3 KB      |
| t2   | stub    | one vector (title + h1 + meta description)          | vector (weak)+keyword | ~3.5 KB    |
| t3   | vault   | no vectors; `page_archives` (Postgres) + metadata   | keyword/title only    | ~0.5 KB    |

t3 is the "glacier": retrieval means a keyword/title hit triggers a restore
(re-fetch, re-embed, back to t0/t1) — seconds, not hours. Consequence: t3
pages live or die by their title/URL metadata.

`PageMsg.depth` (what was fetched) maps 1:1 onto the tier a freshly indexed
page is stored at: `full->t0, summary->t1, headline->t2` (`shared/tiers.py`).
Every point's payload carries `metadata.tier`.

## Heat

```
heat(page) = (hits * view_weight + impressions * impression_weight)
             * 0.5 ** (idle_days / half_life_days)
```

- hits = actual page opens (metering, `page_stats.hits`)
- impressions = times the page appeared in a served result set or AI summary
  (`page_stats.impressions`, fed by the search server alongside POST /hits)
- idle_days = now - last_accessed_at; never accessed -> heat 0
- half_life_days: profile default (news ~14d, reference months), per-partner
  override in `partners.tier_config`

A fresh page starts at t0 by default and decays if nobody touches it; pages
with interest climb back up.

## Transition rules (`shared/tiers.target_tier`)

- One step per evaluation (t0->t1->t2->t3; never t0->t3 in one pass).
- Demotion gated by `min_dwell_days` (default 14d since `last_tier_change`);
  promotion is exempt (a page with real traffic doesn't wait).
- Hysteresis (`TierConfig` defaults):

  ```
  t0->t1 when heat < 20        t3->t2 when heat >= 10
  t1->t2 when heat < 5         t2->t1 when heat >= 25
  t2->t3 when heat < 1         t1->t0 when heat >= 60
  ```

  (floors below thresholds, so a page hovering at a boundary doesn't bounce)

## Service boundaries

The indexer stays Postgres-free and fetch-free, as today. The tier bot
(crawler side, build order 4) owns all state:

```
tier bot (crawler)                          indexer (Qdrant only)
 1. compute heat for pages
 2. decide transition (shared.tiers)
 3. if demote to t3: fetch, extract,
    write page_archives                       <- archive BEFORE delete
 4. publish demote/promote msg
 5. update page_stats (tier, heat,
    last_tier_change)                         delete_url_points /
                                               delete + re-embed + upsert
```

Ordering matters for demote: archive first, then delete. A crash in between
leaves the page both archived and indexed — harmless, the next pass re-demotes
(idempotent). A failed fetch skips the demote (page stays warm — safe default).

Messages (`shared/messages.py`):

- `DemoteMsg {site_id, collection, url, to_tier}` — indexer deletes the page's
  points via payload filter on `metadata.source_url` (payload-indexed).
- `PromoteMsg {site_id, domain, profile, collection, url, title, to_tier,
  sections}` — indexer deletes the page's old points, chunks + embeds the
  provided sections, upserts. Delete-then-upsert is required: point ids are
  `url#section#chunk`, so a different tier has a different chunk count and
  stale points would otherwise orphan.

## Ingest gate + backfill (build order 3 + 6)

New pages from partner feeds default to t0. The gate keeps "extremely old or
uninteresting" pages out of the vector index entirely (straight to t3):

- age > `partners.backfill_cutoff_days` (default 365d) — the onboarding
  backfill uses the same gate, so a 20-year archive costs metadata, not vectors
- thin pages (word count below floor)
- boilerplate paths (`/tag/`, `/author/`, `/category/`, `/page/N`)
- duplicate `content_hash` (syndicated copies)

## Build order

- [x] 1. Schema: `partners` table, `page_stats` tier fields (impressions,
      tier, heat, last_accessed_at, last_tier_change), `page_archives` cold
      store
- [x] 2. Indexer: `DemoteMsg`/`PromoteMsg` handlers, `metadata.source_url`
      payload index, `metadata.tier` on all points, `shared/tiers.py` decision
      module + `tests/test_tiers.py`
- [ ] 3. Feed watcher: per-partner poll (crawl / rss / api adapters) -> ingest
      gate -> PageMsg(t0) or straight to t3
- [ ] 4. Tier bot: scheduled heat evaluation, transition messages,
      page_stats updates
- [ ] 5. tierctl CLI: monitoring/debug (spec below)
- [ ] 6. Backfill job: onboarding crawl with the ingest gate
- [ ] 7. Search side: keyword fallback over page_stats/page_archives so t3
      pages stay reachable, with a restore action
- [ ] 8. Metrics: pages per tier per partner, storage per tier, transition
      rate, restore latency

## tierctl (build order 5)

Basic CLI (`cli/tierctl.py`) for watching the tiering system in action:

```
tierctl status [--partner DOMAIN]    tier distribution + est. storage,
                                     per partner/site
tierctl heat URL                     hits/impressions/idle/decay, computed
                                     heat, current vs expected tier
tierctl dry-run [--partner DOMAIN]   evaluate every page, list planned
                                     transitions, apply nothing
tierctl transitions [--since 24h]    recent tier changes
tierctl promote URL [--tier t0|t1|t2]
tierctl demote URL                   manual transition, same code path as
                                     the bot
```

It calls the same `shared/tiers.py` functions as the bot, so what you see is
what will happen.

## Migration notes (existing deployment)

No alembic in this repo: `create_all` makes the new tables (`partners`,
`page_archives`) on next startup, but the existing `page_stats` table needs:

```sql
ALTER TABLE page_stats ADD COLUMN impressions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE page_stats ADD COLUMN tier VARCHAR(8) NOT NULL DEFAULT 't0';
ALTER TABLE page_stats ADD COLUMN heat DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE page_stats ADD COLUMN last_accessed_at TIMESTAMPTZ;
ALTER TABLE page_stats ADD COLUMN last_tier_change TIMESTAMPTZ;
```

Existing Qdrant points have no `metadata.tier`: treat as t0 until the tier
bot's first pass rewrites or removes them. Existing collections get the
`metadata.source_url` payload index on indexer startup (best-effort).

## Open questions

- `page_archives` in Postgres for v1; move cold text to object storage
  (S3-class) when volume justifies it
- per-partner collection naming (`partners.collection` NULL = share the
  profile's collection)
- impression logging endpoint (search server side, alongside /hits)
