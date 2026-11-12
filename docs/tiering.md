# Partner content tiering

S3-storage-class-style lifecycle for partner content in Qdrant. Partner sites
(`partners` table) are the only sites we index and the only sites we pay out
for. Goal: store hot content fully so vector search finds it, park cold
content where it costs ~10x less, without losing it.

Status: build order 1-8 done (tiering, indexer handlers, feed watcher,
tier bot, tierctl, backfill, keyword search + restore, metrics). See checkboxes below.

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
- half_life_days: default in `config/tiers.yaml` (news ~14d, reference
  months), per-partner block in the same file, transient override in
  `partners.tier_config`

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

1. **compute heat** with the page's partner `TierConfig` (effective config
   from `config/tiers.yaml`, see the Config section below)
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

## Backfill (build order 6)

`crawler/backfill.py` is the onboarding job: one full-site crawl for a new
partner, through the exact same gate + adapters as the feed watcher
(`poll_crawl`). Recent pages index at t0, pages older than the partner's
`backfill_cutoff_days` go straight to the t3 vault, thin/boilerplate/
duplicate pages are skipped — so a 20-year archive costs metadata, not
vectors.

```
python -m crawler.backfill --domain example.com [--max-pages N]
```

- The partner row is onboarded manually first (domain, profile, feed_type,
  backfill_cutoff_days); the `sites` row is linked lazily on first run.
- Idempotent: the gate's unchanged/duplicate/already-archived checks make a
  re-run a no-op, safe for re-pointing a partner and crawling again.
- Hand-off: `partner.last_poll_at` is seeded to now when the crawl
  finishes, so the feed watcher takes over without re-crawling the site.

## Config (config/tiers.yaml)

All tiering parameters live in one declarative file, terraform-style: one
block per declaration, the file is the source of truth.

```yaml
config:
  - type: defaults          # applies to every partner
    view_weight: 1.0
    ...
  - type: partner           # per-partner, matched on partners.domain
    domain: example.com     # declare only what differs from defaults
    half_life_days: 90
```

- Precedence (highest wins): file partner block > `partners.tier_config`
  DB column > file defaults. The DB column stays as a temporary
  per-deployment override; when it and the file disagree, the file wins
  (like `apply` over drifted state).
- `tierctl config [DOMAIN]` shows the effective values, where each one came
  from, and any DB/file drift.
- Adding a new parameter: put the key in the file first (unknown keys are
  accepted and kept), then code the meaning — a field on `TierConfig` plus
  its use in `heat()` / `target_tier()`. No call site changes.

## Search (build order 7)

The search server's tiering slice: vaulted pages must stay reachable even
though their vectors are gone. Two parts.

**Keyword fallback** — `search/keyword.py`, `keyword_search(query,
partner, limit)`. A pure DB query (no network): term matching over
`page_stats` (t0-t2) and the vault (t3 rows + `page_archives`), weighted
url 3.0 > title 2.0 > text 1.0 per distinct matched term, sorted by score,
then heat. A URL in both tables is reported once, labelled by its tier: a
`t3` page comes back as `source: "vault"` with a snippet cut from the
archived copy (or the title, when the term hits the title only); a t0-t2
page as `source: "indexed"`. `partner` filters by domain. This is what
`/search` uses for vaulted hits alongside the vector fan-out (the full
pipeline — Qdrant + CSE/Bing + cross-encoder — lands with the rest of the
search server).

**Restore** — `tierbot.restore_page(url, to_tier)`. Reads the cold copy
from `page_archives` (no live fetch — the page may be gone upstream),
re-publishes a `PromoteMsg` with the exact archived sections at the target
tier, and re-creates the `page_stats` row. The archive row is kept as the
cold copy. Returns `restored` / `no_archive`. The tier bot holds the
restored page on its next pass via the dwell gate; heat re-tiers it from
there.

Entry points: `search/app.py` (`/search?q=&partner=&limit=`,
`/restore?url=&tier=`) and `tierctl restore URL [--tier t0|t1|t2]`.

Schema notes (this step): `page_stats.title` (stamped at index/transition
time — keyword search matches on it), `page_archives.title` +
`page_archives.sections` (the exact archived sections, so restore re-embeds
without a fetch), and `archive_page()` is now an upsert — re-demoting a
changed page refreshes the cold copy.

## Metrics (build order 8)

The system keeps an append-only audit log, `tier_events` (`crawler/db.py::
TierEvent`): one row per applied action — `promote` / `demote` (via `auto`
tier bot or `manual` tierctl), `restore` (with `dwell_hours` = time the page
spent in the vault, i.e. restore latency), and `archive` (pages sent
straight to the vault at ingest). Every applied transition writes one;
holds, no-ops and failed fetches write nothing. Nothing is ever updated or
deleted in the table.

`tierctl metrics [--partner DOMAIN] [--since 30d]` reports:

- **pages per tier per partner** + estimated storage (current state,
  `page_stats`; ~9/3/3.5/0.5 KB per page at t0/t1/t2/t3)
- **storage per tier** across all partners
- **transition rate**: per-kind counts (auto vs manual) and events/day over
  the window
- **restore latency**: min / median / max vault time over the window
- **vault depth**: cold-copy count, median and oldest age (`page_archives`)

`tierctl transitions [--since 24h]` now reads the same log (full history,
newest first) instead of each page's `last_tier_change`.

## Web search integration (CSE / Bing)

The search leg runs two union sources in parallel with the partner index:
keyword/vector over `page_stats` + `page_archives`, and live web search for
content no partner covers. The web provider is deliberately not decided
yet — Google Custom Search and Bing Web Search are interchangeable behind
one interface (`search/web.py`):

- `web_search(query, provider, num)` -> `list[WebHit]` — one hit shape
  (title, url, snippet, source) regardless of provider
- provider picked per-call or via `WEB_SEARCH_PROVIDER` (default `google`);
  keys: `CSE_API_KEY` + `CSE_CX` (Google) or `BING_API_KEY` (Bing)
- missing/empty key -> `ValueError` (misconfiguration, 400); upstream 403/
  429/5xx -> `WebSearchError` (transient, 502) so the union pipeline drops
  that leg for the query instead of failing it
- exposed as `GET /web?q=&provider=&num=` on the search app
- `tests/test_web.py` covers request construction, parsing, dispatch and
  failure paths offline (stubbed HTTP, no keys needed)

## Smoke testing (verification checklist)

`docs/smoke-testing.md` is the end-to-end verification pass for the
tiering system: a fixture partner (`scripts/smoke_server.py`, 7 pages with
controlled publication dates exercising every ingest-gate outcome), traffic
simulation through `POST /hits` (views vs impressions vs sustained loops),
and the full demote -> vault -> restore cycle on a fast-forward config
(`config/smoke-fast.yaml`: ~29-min half-life, ~2.5-min dwell, so the whole
lifecycle completes in minutes instead of weeks). Section 9 of the same
doc walks the live web-search leg for both providers.

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
- [x] 6. Backfill job: onboarding crawl with the ingest gate
      (`crawler/backfill.py`, `tests/test_backfill.py`)
- [x] 7. Search side: keyword fallback over page_stats/page_archives so t3
      pages stay reachable, with a restore action (`search/`,
      `tierbot.restore_page`, `tests/test_keyword.py`)
- [x] 8. Metrics: pages per tier per partner, storage per tier, transition
      rate, restore latency (`tier_events` log, `tierctl metrics`)

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
tierctl transitions [--since 24h]    recent tier events (the tier_events
                                     log, newest first)
tierctl metrics [--partner DOMAIN]   pages/storage per tier, transition
  [--since 30d]                        rate, restore latency, vault depth
tierctl config [DOMAIN]              effective tiering parameters,
                                     per-parameter provenance + drift
tierctl promote URL [--tier t0|t1|t2]
tierctl demote URL                   manual transition, same code path as
                                     the bot
tierctl restore URL [--tier t0|t1|t2] restore a vaulted page from
                                     page_archives (re-embeds the archived
                                     copy, no live fetch)
```

It calls the same `shared/tiers.py` functions as the bot, so what you see is
what will happen.

## Migration notes (existing deployment)

No alembic in this repo: `create_all` makes the new tables (`partners`,
`page_archives`, `tier_events`) on next startup, but the existing
`page_stats` table needs:

```sql
ALTER TABLE page_stats ADD COLUMN impressions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE page_stats ADD COLUMN tier VARCHAR(8) NOT NULL DEFAULT 't0';
ALTER TABLE page_stats ADD COLUMN heat DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE page_stats ADD COLUMN last_accessed_at TIMESTAMPTZ;
ALTER TABLE page_stats ADD COLUMN last_tier_change TIMESTAMPTZ;
ALTER TABLE page_stats ADD COLUMN title VARCHAR(2048);
-- only if page_archives already existed from an earlier deployment:
ALTER TABLE page_archives ADD COLUMN title VARCHAR(2048);
ALTER TABLE page_archives ADD COLUMN sections JSON;
```

Existing Qdrant points have no `metadata.tier`: treat as t0 until the tier
bot's first pass rewrites or removes them. Existing collections get the
`metadata.source_url` payload index on indexer startup (best-effort).

If the database predates the Cloudflare Radar removal, two `sites` columns
are orphaned (optional to drop — the models no longer read or write them):

```sql
ALTER TABLE sites DROP COLUMN IF EXISTS rank;
ALTER TABLE sites DROP COLUMN IF EXISTS category;
```

## Open questions

- `page_archives` in Postgres for v1; move cold text to object storage
  (S3-class) when volume justifies it
- per-partner collection naming (`partners.collection` NULL = share the
  profile's collection)
- impression logging endpoint (search server side, alongside /hits)
- web search provider choice (Google CSE vs Bing) — code is provider-
  neutral; the decision is an env var, not a code change
- smoke runbook live sections (1-8, 9) need a real run against the shared
  Postgres/Redis stack with API keys to certify
