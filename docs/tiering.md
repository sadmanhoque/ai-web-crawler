# Partner content tiering

S3-storage-class-style lifecycle for partner content in Qdrant. Partner sites
(`partners` table) are the only sites we index and the only sites we pay out
for. Goal: store hot content fully so vector search finds it, park cold
content where it costs ~10x less, without losing it.

Status: build order 1-5 done (tiering, indexer handlers, feed watcher,
tier bot, tierctl). See checkboxes below.

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

New pages from partner feeds default to t0. The gate (`shared/gate.py`, pure
function, checked in `tests/test_gate.py`) keeps "extremely old or
uninteresting" pages out of the vector index entirely. Check order:

1. **unchanged** — `page_stats.content_hash` already matches -> skip
   (re-crawls of unchanged pages never re-embed)
2. **boilerplate path** — `/tag/`, `/author/`, `/category/`, `/page/N`,
   `/search`, ... -> skip (substring heuristic, see `shared/gate.BOILERPLATE`)
3. **thin** — word count below `GATE_MIN_WORDS` (default 60) -> skip.
   Applies to full-page crawls only; rss/api items are summaries by design,
   so feed adapters pass `min_words=None`
4. **age** — `published` (from `article:published_time` / JSON-LD /
   `<time datetime>`) older than `partners.backfill_cutoff_days` (default
   365d) -> straight to t3. Unknown publication date is treated as fresh
   (index now, archive later via heat)
5. **duplicate** — another URL (indexed or archived) already holds this
   exact content_hash (syndicated copies) -> skip

The onboarding backfill (step 6) uses the same gate, so a 20-year archive
costs metadata, not vectors.

## Feed watcher (build order 3)

`crawler/feedwatcher.py` runs as `FEED_WORKERS` threads in the crawler app.
`claim_due_partner()` is the SKIP-LOCKED twin of the scheduler's
`claim_due()`; partner-linked `sites` rows are excluded from the scheduler
so a partner is only ever crawled by the feed watcher. Per poll:

- **crawl** adapter: BFS via `SiteCrawler`, gate with the thin floor;
  new/changed pages publish at t0 (depth full), old pages archive to t3
- **rss** adapter: RSS 2.0 + Atom via stdlib `xml.etree` (no new deps);
  item text = `content:encoded`/`description` (RSS) or `content`/`summary`
  (Atom), HTML-stripped
- **api** adapter: generic JSON feed (a list or `{"items": [...]}`) with
  `url|link`, `title`, `published|publishedAt|pubDate`,
  `content|summary|description`

Publishing a page immediately upserts its `page_stats` row with the
content_hash, so the next poll dedupes before the first hit ever lands.
Archived pages are marked in `page_archives` the same way.

**No CloseMsg for partner crawls, on purpose:** a `complete` crawl would
delete points the gate intentionally left unindexed (t3 archives, boilerplate
skips). Deletion of partner pages is the tier bot's job; points of pages a
partner removed are reclaimed by heat decay -> demote.

## Tier bot (build order 4)

`crawler/tierbot.py` runs as one thread in the crawler app (`TIER_BOT=0` to
disable, `TIER_EVAL_MINUTES` for the interval, default 60). Each pass
(`evaluate_pass`) evaluates every page of every enabled partner — exactly the
rows tierctl's dry-run inspects:

1. **compute heat** with the page's partner `TierConfig` (per-partner
   overrides in `partners.tier_config`, defaults in `shared/tiers.py`)
2. **decide** via `shared.tiers.target_tier` (one step per pass, dwell gate
   on demote, hysteresis floors/thresholds)
3. **fetch the page** — archive text and re-embed sections must come from a
   live copy; a failed fetch skips the transition (page keeps its tier, the
   safe direction both ways)
4. **publish + update**: demote to t3 archives to `page_archives` FIRST
   (archive-before-delete; a crash in between is harmless and re-demotes),
   then `DemoteMsg`; any move that keeps vectors publishes `PromoteMsg`
   (delete old points, re-embed the target tier's representation: t0 all
   sections, t1 the lede section, t2 one stub of title + h1 + meta
   description). page_stats (tier, heat, last_tier_change) is committed
   after the message goes out.

`heat()` refreshes on every pass even for held pages, so `tierctl heat`
always shows the current decayed score.

Metering (`POST /hits`): a hit stamps `last_accessed_at` (a page open IS an
access — without it heat stayed 0 no matter the hit count) and accepts an
`impressions` count. A first hit on a vaulted page re-creates its
`page_stats` row **at t3** (not the t0 default), so heat promotes it back up
instead of demoting a hot page.

`tierctl promote/demote` run the same `transition_page` pipeline as the bot
(fetch, archive, message, page_stats) — the operator just overrides the
dwell gate and heat thresholds (one step, or `--tier` to jump).

## Build order

- [x] 1. Schema: `partners` table, `page_stats` tier fields (impressions,
      tier, heat, last_accessed_at, last_tier_change), `page_archives` cold
      store
- [x] 2. Indexer: `DemoteMsg`/`PromoteMsg` handlers, `metadata.source_url`
      payload index, `metadata.tier` on all points, `shared/tiers.py` decision
      module + `tests/test_tiers.py`
- [x] 3. Feed watcher: per-partner poll (crawl / rss / api adapters) -> ingest
      gate -> PageMsg(t0) or straight to t3 (`crawler/feedwatcher.py`,
      `shared/gate.py`, `tests/test_gate.py`)
- [x] 4. Tier bot: scheduled heat evaluation, transition messages,
      page_stats updates (`crawler/tierbot.py`, `tests/test_tierbot.py`)
- [x] 5. tierctl CLI: monitoring/debug (`cli/tierctl.py`; dry-run mirrors
      `evaluate_pass`, promote/demote run the bot's pipeline)
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
tierctl dry-run [--partner DOMAIN]   evaluate partner pages (the same rows
                                     the bot sees), list planned
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
