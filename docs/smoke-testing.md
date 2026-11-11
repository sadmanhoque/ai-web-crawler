# Partner Tiering — Verification Checklist

Deeper pass than `docs/tiering.md` — this exercises the tiering system
end to end against **simulated traffic**: real pages with controlled
publication dates, injected clicks and impressions, and the full
demote → vault → restore cycle, so the system is verified with its
actual decision paths, not unit tests.

The web search leg (Google CSE / Bing) is covered in section 9 — the
provider is still an open product decision, so both are wired behind one
interface and both are tested.

## Prerequisites

Everything runs on ONE machine (the laptop), except Postgres, which stays
in the shared `localmind` k8s cluster and is reached with a
port-forward. This keeps `127.0.0.1:8801` valid from every process.

```bash
# Terminal 0 — shared Postgres (the pg-crawler-db-init job created role + db
# localmind_crawler in the cluster; credentials from localmind-crawler-pg):
kubectl port-forward -n localmind svc/localmind-pg 5433:5432 &
PGPASS=$(kubectl -n localmind get secret localmind-crawler-pg -o jsonpath='{.data.PASSWORD}' | base64 -d)

# Terminal 1 — Redis (the message bus between crawler and indexer):
docker run -d --name tiering-smoke-redis -p 6379:6379 redis:7

# Terminal 2 — the fixture partner (scripts/smoke_server.py):
python3 scripts/smoke_server.py          # http://127.0.0.1:8801
```

Shell variables for every tab that uses them:

```bash
export DATABASE_URL="postgresql+psycopg://localmind_crawler:$PGPASS@localhost:5433/localmind_crawler"
export REDIS_URL=redis://localhost:6379/0
export TIERS_FILE=config/tiers.yaml
```

Terminal 3 — the crawler app with the tier bot, fast-forwarded (section 5
explains why; use `config/smoke-fast.yaml`):

```bash
TIERS_FILE=config/smoke-fast.yaml TIER_EVAL_MINUTES=1 \
uvicorn crawler.app:app --port 8088
```

Terminal 4 — the search app:

```bash
uvicorn search.app:app --port 8089
export CRAWLER=http://localhost:8088
export SEARCH=http://localhost:8089
export PSQ() { PGPASSWORD="$PGPASS" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler -c "$1"; }
```

`tierctl` runs directly on this machine (it only needs `DATABASE_URL`):

```bash
alias tierctl='python3 cli/tierctl.py'
```

The fixture partner (one terminal) serves pages with controlled dates:
`/hot` and `/fresh` (today), `/warm` (30 days ago), `/old` (700 days
ago — older than the default `backfill_cutoff_days=365`), `/thin`
(~6 words, under the 60-word floor), `/tag/legacy` (boilerplate path),
`/` (thin landing) and `/feed` (RSS of hot/warm/old).

---

## 1. Onboard the fixture partner — the ingest gate's four outcomes

One backfill must produce every gate decision at once: index, straight to
vault, thin-skip, boilerplate-skip.

```bash
PSQ "INSERT INTO partners (domain, profile, feed_type)
     VALUES ('127.0.0.1:8801', 'docs', 'crawl');"

# crawler side (this machine):
python3 -m crawler.backfill --domain 127.0.0.1:8801
```

**Expect: `{'index': 3, 'archive': 1, 'skip': 3}`** — hot/fresh/warm
indexed, old archived, thin + boilerplate + thin landing skipped.

Check the actual rows, not just the stats:

```bash
PSQ "SELECT url, tier FROM page_stats ORDER BY url;"
# /fresh t0, /hot t0, /warm t0  — and nothing else
PSQ "SELECT url, tier, left(text, 40) FROM page_archives;"
# /old t3, with its full body text
PSQ "SELECT kind, to_tier, via FROM tier_events ORDER BY id;"
# one 'archive' event: the /old page went straight to the vault at ingest
tierctl status
```

Re-run the backfill — **expect the same counts as no-ops:
`{'index': 0, 'archive': 0, 'skip': 7}`** (idempotence: unchanged hashes,
already-archived, and the skips).

## 2. Simulating traffic — `/hits` is the only input that moves heat

The search server reports real usage to `POST /hits` as a batch. Everything
the tier bot does downstream is driven by these two numbers:

| What you want to simulate | How | What changes |
|---|---|---|
| A user opens the page (a **view**) | `[{"url": "...", "site_domain": "127.0.0.1:8801", "count": N}]` | `hits += N`, `last_accessed_at = now`, heat gains `N × view_weight` (1.0), decayed |
| The page appears in a result set / AI answer (**impression**, incl. AI-citation billing events) | same body + `"impressions": N` | `impressions += N`, heat gains `N × impression_weight` (0.1) |
| A whole result set (your real server's per-query report) | one batch, one entry per served page, each with its `count` + `impressions` | one commit |
| Sustained interest (days/weeks) | the loop below | heat accumulates and keeps refreshing `last_accessed_at`, so decay can't outpace it |
| Neglected content | do nothing — heat decays by `0.5^(idle_days / half_life_days)` (default 14d), or fast-forward (section 5) | the tier bot demotes on a later pass |

```bash
# 25 opens of /hot => heat 25, which is ABOVE t0_floor (20): it must stay t0
curl -s -X POST $CRAWLER/hits -H 'Content-Type: application/json' -d '[
  {"url": "http://127.0.0.1:8801/hot", "site_domain": "127.0.0.1:8801", "count": 25}]'
# => {"ok":1}

# one result set: /warm viewed, /fresh only surfaced
curl -s -X POST $CRAWLER/hits -H 'Content-Type: application/json' -d '[
  {"url": "http://127.0.0.1:8801/warm",  "site_domain": "127.0.0.1:8801", "count": 3},
  {"url": "http://127.0.0.1:8801/fresh", "site_domain": "127.0.0.1:8801", "impressions": 40}]'

# a week of steady background interest in /warm (1 open a day, 10 impressions a day)
for i in $(seq 1 7); do
  curl -s -X POST $CRAWLER/hits -H 'Content-Type: application/json' -d '[
    {"url": "http://127.0.0.1:8801/warm", "site_domain": "127.0.0.1:8801",
     "count": 1, "impressions": 10}]' > /dev/null
  sleep 3
done
```

Heat is `decayed(hits × 1.0 + impressions × 0.1)`. For any URL:

```bash
tierctl heat http://127.0.0.1:8801/hot
# hits, impressions, idle time, the decay factor, computed heat,
# and current tier vs the tier the thresholds expect
```

**Expect:** `/hot` heat ≈ 25 (≥ t0_floor 20 → holds t0), `/fresh` heat ≈
4.0 (40 × 0.1 — impressions alone can't hold a page → expected tier t1),
`/warm` heat ≈ 17 (10 × 1.0 + 70 × 0.1 → below the floor → expected tier
t1). Note the rhythm: **one step per pass** — a page cascades one tier per
evaluation, never jumps.

## 3. The tier bot acts on the next pass — demote the cold, keep the hot

With the fast-forward config (section 5) a pass lands within a minute —
but the first pass that can *act* is ~3 minutes after the backfill (the
fast min-dwell is ~2.5 minutes). Preview first — the dry-run shows the
same rows the bot sees and applies nothing:

```bash
tierctl dry-run
```

**Expect it to plan:** `/fresh` and `/warm` (and whatever else cooled
below its floor) demoted one step; `/hot` held (heat 25 ≥ t0_floor 20).

Wait for a pass (watch `TIER_EVAL_MINUTES`), then check the state, not
the log:

```bash
tierctl status                      # /hot still t0; /warm /fresh dropped one tier
tierctl transitions --since 1h      # one 'demote ... via auto' event per applied move
PSQ "SELECT url, tier, round(heat,2) FROM page_stats ORDER BY url;"
tierctl metrics                     # transition counts, storage per tier, vault depth
```

Let it cool further (re-run the dry-run after each pass): `/warm` and
`/fresh` should cascade t1 → t2 → t3. At t3 the vectors are gone but the
page must stay reachable — verify in section 4.

## 4. Vaulted pages stay findable — and a hit brings them back up

```bash
# keyword leg (search side): the vaulted /old page answers a body-term query
curl -s "$SEARCH/search?q=delta&partner=127.0.0.1:8801" | python3 -m json.tool
# expect: source "vault", tier "t3", a snippet around the word
```

Now simulate a user hitting a vaulted page — the search server would call
`/restore`, or the page simply gets opened:

```bash
# A: the page is opened (the product path): /hits re-creates the row AT t3,
# not at the t0 default — so the bot promotes it up through heat instead of
# demoting a hot page.
curl -s -X POST $CRAWLER/hits -H 'Content-Type: application/json' -d '[
  {"url": "http://127.0.0.1:8801/old", "site_domain": "127.0.0.1:8801", "count": 12}]'
PSQ "SELECT url, tier, hits FROM page_stats WHERE url LIKE '%/old';"
# tier t3, hits 12  => heat 12 ≥ t2_promote (10): next pass promotes t3 -> t2
```

Then re-run the query from the first block — **the same URL must now
appear with `source: "indexed"` and tier t2** (the dedup: one row in both
`page_stats` and `page_archives` is reported once, labelled by tier).

**B: the restore action** (re-embed from the archive, no live fetch — for
when the page may be gone upstream):

```bash
curl -s -X POST $SEARCH/restore -H 'Content-Type: application/json' \
  -d '{"url": "http://127.0.0.1:8801/old", "tier": "t0"}'
# => {"result": "restored", ...}
tierctl metrics
# 'restore latency' section: n=1, and min/median/max = the time the page
# spent in the vault (its archived_at -> now)
```

## 5. Fast-forwarding time — the whole lifecycle in minutes, not weeks

The production defaults (14-day half-life, 14-day min dwell) are correct
for production; they are unusable for a smoke test. The tiering config is
declarative (build order: `config/tiers.yaml`), so the fast-forward is a
file, not a code change:

```yaml
# config/smoke-fast.yaml
config:
  - type: defaults
    view_weight: 1.0
    impression_weight: 0.1
    half_life_days: 0.02        # ~29 minutes: one idle hour halves the heat
    min_dwell_days: 0.0017      # ~2.5 minutes: anti-flapping gate, short
                                # enough that the first pass can act
    t0_floor: 20
    t1_floor: 5
    t2_floor: 1
    t2_promote: 10
    t1_promote: 25
    t0_promote: 60
```

Start the crawler with `TIERS_FILE=config/smoke-fast.yaml
TIER_EVAL_MINUTES=1` (see Prerequisites). Verify the override is live:

```bash
tierctl config        # shows the file path + effective values
tierctl dry-run       # 'expected' tiers now reflect the fast decay
```

Remove the override (restart with the stock `config/tiers.yaml`) when the
smoke is done — the DB column stays clean because nothing was written to
it (file wins, like `apply`).

## 6. Manual transitions — the operator path, same pipeline

```bash
tierctl demote http://127.0.0.1:8801/hot     # t0 -> t1 (keeps vectors, lede only)
tierctl demote http://127.0.0.1:8801/hot     # t1 -> t2 (one stub)
tierctl demote http://127.0.0.1:8801/hot     # t2 -> t3 (archived, vectors deleted)
tierctl transitions --since 1h
# three 'demote ... via manual' events — manual and auto share the log
tierctl restore http://127.0.0.1:8801/hot --tier t0
# re-embeds the ARCHIVED sections (the text crawled at demote time), no fetch
curl -s "$SEARCH/search?q=alpha" | python3 -m json.tool   # 'alpha' = /hot's body word
# back as source "indexed", tier "t0"
```

**Expect** the restore's `tier_events` row to carry `dwell_hours` ≈ the
minutes /hot spent in the vault, and `tierctl metrics` to report it under
restore latency.

## 7. The audit trail — what "fully verified" checks

```bash
PSQ "SELECT kind, via, count(*) FROM tier_events GROUP BY 1, 2 ORDER BY 1, 2;"
# every applied action is logged exactly once; holds/no-ops/fetch failures: none
PSQ "SELECT count(*) FROM page_archives;  "   # cold copies (vaulted + restored-then-revaulted)
tierctl metrics --partner 127.0.0.1:8801
```

Cross-check each metrics section against what you just did: pages per tier
matches `page_stats`, storage per tier is `count × ~9/3/3.5/0.5 KB`, the
transition breakdown counts auto vs manual separately, restore latency
exists iff section 4B/6 restored something, and the vault section's ages
match `page_archives.archived_at`.

## 8. Config provenance — changing a parameter changes behavior

```bash
# add a partner block to config/tiers.yaml:
#   - type: partner
#     domain: 127.0.0.1:8801
#     t0_floor: 5
tierctl config 127.0.0.1:8801
# t0_floor shows [file: partner block] while every other param shows
# [file: defaults]; DB/file drift would be flagged here
tierctl dry-run
# /hot (heat ~25) was held by the floor 20; with floor 5 it's still fine —
# use a hotter floor test: set t0_floor: 30 and watch /hot get demoted
```

## 9. Web search leg — CSE vs Bing (provider still an open decision)

Both providers sit behind one interface (`search/web.py`) returning the
same hit shape; `WEB_SEARCH_PROVIDER` (or the `?provider=` param) picks
per deployment, so deciding google-vs-bing later is a config change, not
code. The search app exposes the leg at `GET /web`:

```bash
# 9a. Misconfiguration must fail loudly, not silently (negative test FIRST):
curl -s -o /dev/null -w "%{http_code}\n" "$SEARCH/web?q=kubernetes"
# Expect: 400 — "google web search needs CSE_API_KEY and CSE_CX"
```

Google Custom Search JSON API:

```bash
# keys: Custom Search JSON API key + a Programmable Search Engine (cx) id
export CSE_API_KEY=... CSE_CX=... WEB_SEARCH_PROVIDER=google
curl -s "$SEARCH/web?q=kubernetes+autoscaling" | python3 -m json.tool
# 200, results[] with title/url/snippet/source "google-cse"
```

Bing Web Search:

```bash
export BING_API_KEY=... WEB_SEARCH_PROVIDER=bing
curl -s "$SEARCH/web?q=kubernetes+autoscaling" | python3 -m json.tool
# 200, source "bing"
```

**Expect 502 with the upstream status** (not a 200 with junk) if a key
is set but rejected/quota-exceeded — that's how the union pipeline knows
to drop this leg for the query instead of failing it. Both providers are
also covered offline by `python3 tests/test_web.py` (request construction,
response parsing, pagination clamps, 403/429 handling, dispatch) — run it
any time, no keys needed.

---

## What "fully verified" means here

Every section above passing means: content enters only through the gate
(old → vault, thin/boilerplate → never indexed), heat is the only thing
that moves a page, decay really cools neglected pages through
t0 → t1 → t2 → t3 while sustained traffic holds them, a vaulted page
never disappears (keyword leg + restore bring it back, and a hit on a
vaulted page re-creates its row at t3, not t0), every applied action is
audited exactly once with real latency numbers, and a config edit changes
measurable behavior with visible provenance. That's the bar for "the
tiering system works," not "the happy path demo works."

## Reading the outputs

| You see | Meaning | Fix |
|---|---|---|
| `dry-run` plans nothing but pages should move | heat is below/above floors differently than you think | `tierctl heat URL` — inspect hits/impressions/idle/decay for that URL |
| a page is "held" and stays held across passes | the min-dwell anti-flapping gate | expected right after a move; fast-forward `min_dwell_days` (section 5) |
| `backfill` stats all-zero on re-run | idempotence working | nothing — that's the point |
| `/hits` returns `{"ok":N}` but the page never moves | `ok` is the batch size — a `site_domain` matching no site row is silently dropped | check `PSQ "SELECT hits FROM page_stats WHERE url LIKE '%/<slug>';"` — use the exact registered domain (`127.0.0.1:8801`) |
| `tierctl metrics` shows no restore latency | nothing was restored in the window | widen `--since` or run section 4B/6 |
| `/web` → 400 | provider key missing/empty | set the env vars for the provider (section 9) |
| `/web` → 502 | key set but the upstream rejected it (403/429/5xx) | check the status in the body: wrong project, quota, or plan |
| tier bot logs a pass but nothing changed | every page held (dwell or thresholds) | compare `tierctl dry-run` with `tierctl heat` per page |
