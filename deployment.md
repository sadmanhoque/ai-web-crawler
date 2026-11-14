# ai-web-crawler on minikube — deployment reference

Single step-by-step reference for a fresh minikube deploy of the
**ai-web-crawler** stack, in the same style as
`ai-web-search/dev-env/ai-gen-deployment-steps`.

The crawler is **its own server**. It shares exactly two things with the
ai-web-search deployment, and only because it deliberately does:

* the **Postgres cluster** — the CNPG cluster `localmind-pg` deployed by the
  ai-web-search repo. The crawler gets its **own database** (`localmind_crawler`)
  and **own login role** inside that cluster, created by the ai-web-search
  repo's one-shot Job `pg-crawler.yaml`. Same cluster = one HA setup, one
  backup, one monitoring surface; separate database = clean ownership and
  least privilege (the crawler can never touch the app's `localmind` DB with
  users/subscriptions, and vice versa).
* the **Qdrant** — the `qdrant` Helm release deployed by the ai-web-search
  repo. The crawler's indexer **writes** per-profile collections
  (`general`, `docs`, `encyclopedia`, `news` — see `config/profiles.yaml`);
  the ai-web-search app **reads** them for vector search.

Everything else is owned by THIS repo: Redis (the message bus), the crawler,
indexer and search Deployments, the crawler's ConfigMap + token Secret, and
the crawler's images.

> **Assumption:** the ai-web-search/dev-env deployment (the "server") is
> **already deployed and healthy** — that is the deployment order by design:
> the server goes first, the crawler comes after. Everything the crawler
> shares (the Postgres cluster, the crawler's database + role, Qdrant, the
> two shared Secrets) is created by that deployment. If any part of it is
> missing, stop and finish that deployment first
> (`ai-web-search/dev-env/ai-gen-deployment-steps`) — this doc only *uses*
> the shared resources, it does not create them.
>
> Section 3 is a quick read-only pre-check that confirms the assumption.

---

## 0. Topology

```
 ai-web-search repo (shared, deployed FIRST)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ localmind-pg  (CNPG cluster, namespace localmind)                    │
 │   ├─ db localmind          (search app: users, subscriptions, ...)   │
 │   └─ db localmind_crawler  (role+db created by pg-crawler.yaml Job)  │
 │ qdrant        (Helm release, svc qdrant:6333,                       │
 │                API key = localmind-secrets/qdrant-api-key)           │
 │ secrets: localmind-secrets, localmind-crawler-pg                     │
 └──────────────────────────────────────────────────────────────────────┘
      ▲  upserts vectors per collection      ▲ connects
      │                                      │
 ai-web-crawler repo (THIS doc)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ localmind-crawler ──XADD──► redis ──consumer group──► localmind-indexer
 │  (API + crawl workers,   index:events   (embeds chunks,             │
 │   feed worker, tier bot)  index:dead     upserts into Qdrant)        │
 │  port 8000 /healthz       group indexers                             │
 │  /hits  /admin/*                                                │
 │ localmind-search ── same DB + same redis (keyword leg, /restore,    │
 │  port 8000 /health, /web)  /restore republishes to the indexer)     │
 └──────────────────────────────────────────────────────────────────────┘
```

Data flow for one page: feedwatcher discovers a new page on a partner site
→ crawl worker fetches/extracts it (Playwright in the crawler image) →
`PageMsg` XADD'd to `index:events` → indexer chunks it, embeds it with the
profile's sentence-transformers model, upserts points into the profile's
Qdrant collection → tier bot scores pages by heat and demotes/promotes
(demote to t3 writes the page text to `page_archives` and deletes its
vectors; `/restore` re-embeds from the archive on demand).

## 1. Object ownership

| Object | Namespace | Owner | Notes |
|---|---|---|---|
| CNPG operator + `localmind-pg` cluster | localmind | **ai-web-search** (`cngp.local.yaml` for minikube) | shared Postgres |
| Job `pg-crawler-db-init` | localmind | **ai-web-search** (`pg-crawler.yaml`) | creates role+db `localmind_crawler`; **must be Complete before the crawler pod starts** |
| Secret `localmind-crawler-pg` | localmind | **ai-web-search** (`secrets.dev.yaml`) | `user`, `password`, `dbname`, `host`, `port`, `database-url` |
| Helm release `qdrant` (+ svc `qdrant`) | localmind | **ai-web-search** | shared vector DB |
| Secret `localmind-secrets` | localmind | **ai-web-search** | `qdrant-api-key`, Stripe, JWT, … — the crawler only reads `qdrant-api-key` from it |
| ConfigMap `localmind-config` | localmind | **ai-web-search** | the search app's env — **do not reuse the name** |
| ConfigMap `localmind-crawler-config` | localmind | **this repo** (`k8s/configmap.md`) | `profiles.yaml`, `sites.yaml`, `tiers.yaml` |
| Secret `localmind-crawler-secret` | localmind | **this repo** (`k8s/configmap.md`) | `admin-token`, `anthropic-key`, `cse-api-key`, `cse-cx`, `bing-api-key` |
| Deployment/Service `redis` | localmind | **this repo** (`k8s/redis.yaml`) | message bus, dev-grade (no persistence) |
| Deployment/Service `localmind-crawler` | localmind | **this repo** (`k8s/crawler.yaml`) | + HPA (1–8) |
| Deployment `localmind-indexer` | localmind | **this repo** (`k8s/indexer.yaml`) | + optional KEDA ScaledObject (`k8s/indexer-keda.yaml`) |
| Deployment/Service `localmind-search` | localmind | **this repo** (`k8s/search.yaml`) | keyword/restore/web slice |

Why the names are split this way: two objects with the same name in one
namespace are the *same* object — the last apply wins and the other app
silently breaks. So the crawler repo never reuses a name the search repo
owns, and vice versa.

## 2. Prerequisites

* minikube running, ≥ 4 CPU / ≥ 11 GiB RAM recommended (the indexer image
  pulls torch; the indexer requests 3 GiB). kubectl available for everything
  in this doc; helm only for the optional KEDA step (section 11).
* The ai-web-search/dev-env deployment is complete and healthy (the
  assumption above — section 3 verifies it in one block).
* This repo (`ai-web-crawler`) at or after the tiering build (steps 1–8):
  `crawler/`, `indexer/`, `search/`, `shared/`, `config/`, `k8s/`.
* Outbound internet from the cluster (the first crawl downloads the
  sentence-transformers embedding models from Hugging Face — see gotcha #5).

## 3. Step 1 — pre-check: the shared resources exist (assumed — verify)

Everything checked below is owned by the ai-web-search deployment and is
**assumed to already exist** — this is a fast read-only pass. Every line
must hold before you apply anything from this repo; if one is red, the
server deployment is incomplete — fix it over there
(`ai-web-search/dev-env/ai-gen-deployment-steps`), not in this doc:

```bash
# 1. Postgres cluster is up (CNPG generates the app secret when ready)
kubectl -n localmind get cluster localmind-pg
kubectl -n localmind get secret localmind-pg-app

# 2. The crawler's database + role exist in that cluster — the Job that
#    creates them (part of the server deployment) must be COMPLETE.
#    If this line is red, the server deployment is incomplete:
kubectl -n localmind get job pg-crawler-db-init
#   NAME                COMPLETIONS   DURATION
#   pg-crawler-db-init  1/1           8s
#   (missing/failed -> apply from the search repo:
#    kubectl apply -f <ai-web-search>/k8s-manifests/pg-crawler.yaml
#    kubectl -n localmind wait --for=condition=complete job/pg-crawler-db-init --timeout=120s
#    if FAILED: kubectl -n localmind logs job/pg-crawler-db-init)

# 3. The two secrets the crawler stack consumes
kubectl -n localmind get secret localmind-crawler-pg
kubectl -n localmind get secret localmind-secrets
#   localmind-secrets must contain the key qdrant-api-key:
kubectl -n localmind get secret localmind-secrets -o jsonpath='{.data.qdrant-api-key}' | base64 -d; echo

# 4. The shared Qdrant release + service
helm list -n localmind | grep qdrant
kubectl -n localmind get svc qdrant
```

## 4. Step 2 — crawler-only config + secrets (this repo)

```bash
cd <ai-web-crawler-repo>
# ConfigMap: profiles + sites + tiering config, mounted at /config in every pod
kubectl create configmap localmind-crawler-config \
  --namespace localmind \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml --from-file=config/tiers.yaml

# Token secret. Empty values are valid and mean "feature off":
#   admin-token  empty  -> crawler /hits + /admin/* have no auth (dev only)
#   anthropic-key empty -> page summaries disabled
#   cse/bing keys empty -> /web returns 400 (by design: misconfiguration)
kubectl create secret generic localmind-crawler-secret \
  --namespace localmind \
  --from-literal=admin-token=CHANGEME \
  --from-literal=anthropic-key= \
  --from-literal=cse-api-key= \
  --from-literal=cse-cx= \
  --from-literal=bing-api-key=

# (Re-running kubectl create on existing objects fails with "AlreadyExists" —
#  kubectl delete configmap/localmind-crawler-config secret/localmind-crawler-secret
#  -n localmind first, or switch to kubectl apply with a rendered YAML.)
```

## 5. Step 3 — Redis (this repo)

```bash
kubectl apply -f k8s/redis.yaml
kubectl -n localmind rollout status deploy/redis --timeout=90s
```

## 6. Step 4 — build the images

All three with `minikube image build`, not `docker build` + load: the host
docker path OOMs on the torch install on a small VM (same gotcha as the
ai-web-search doc, #2).

```bash
cd <ai-web-crawler-repo>

# 1. Crawler — based on the Playwright image (headless browsers preinstalled);
#    the largest one (~2-3 GiB).
minikube image build -f crawler/Dockerfile -t registry.example.com/localmind-crawler:latest .

# 2. Indexer — sentence-transformers pulls the CPU torch wheels (slowest step).
minikube image build -f indexer/Dockerfile -t registry.example.com/localmind-indexer:latest .

# 3. Search — plain python, quick.
minikube image build -f search/Dockerfile -t registry.example.com/localmind-search:latest .

minikube image ls | grep localmind
```

The `registry.example.com/...:latest` tags are placeholders; no real registry
is involved — every pod runs `imagePullPolicy: IfNotPresent`, so kubelet uses
the image from minikube's store.

## 7. Step 5 — apply the crawler stack (this repo)

```bash
kubectl apply -f k8s/redis.yaml      # step 3, shown for the one-shot flow
kubectl apply -f k8s/crawler.yaml    # Deployment + Service + HPA
kubectl apply -f k8s/indexer.yaml    # Deployment (consumer group; 1 replica is fine for dev)
kubectl apply -f k8s/search.yaml     # Deployment + Service
# kubectl apply -f k8s/indexer-keda.yaml    # OPTIONAL — only if KEDA is installed (section 11)

kubectl -n localmind rollout status deploy/localmind-crawler --timeout=180s
kubectl -n localmind rollout status deploy/localmind-indexer --timeout=300s
kubectl -n localmind rollout status deploy/localmind-search  --timeout=120s
```

Small VM (11 GiB): if the crawler pods sit Pending,
`kubectl -n localmind scale deploy/localmind-crawler --replicas=1`
(manifest ships with 2).

## 8. Step 6 — verification

### 8.1 Pods

```bash
kubectl -n localmind get pods -l 'app in (redis,localmind-crawler,localmind-indexer,localmind-search)'
# all Running/Ready. Crawler startup log:
kubectl -n localmind logs deploy/localmind-crawler --tail=30
#   init_db + sync-manual lines, then the worker threads start
```

### 8.2 HTTP endpoints

```bash
# crawler API (svc port 80 -> container 8000)
kubectl port-forward -n localmind svc/localmind-crawler 8000:80 &
curl -s localhost:8000/healthz                 # {"ok":true}
curl -s -X POST localhost:8000/admin/sync-manual -H "Authorization: Bearer CHANGEME"
#   {"synced": 0}   (0 until the first partner exists)

# search slice
kubectl port-forward -n localmind svc/localmind-search 8001:80 &
curl -s localhost:8001/health                  # {"ok":true}
curl -s "localhost:8001/search?q=test"         # {"query":"test","results":[]} — empty until first crawl
```

### 8.3 Postgres (shared cluster, crawler's database)

```bash
kubectl port-forward -n localmind svc/localmind-pg 5433:5432 &
PGPASS=$(kubectl -n localmind get secret localmind-crawler-pg -o jsonpath='{.data.password}' | base64 -d)
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler -c "\dt"
#    List of Relations
#  Schema |    Name     | Type |       Owner
# -------+-------------+------+-----------------
#  public | page_archives | table | localmind_crawler
#  public | page_stats    | table | localmind_crawler
#  public | partners      | table | localmind_crawler
#  public | sites         | table | localmind_crawler
#  public | tier_events   | table | localmind_crawler
#   (created by init_db() on the crawler pod's first start — empty before that)
```

### 8.4 Qdrant (shared release)

```bash
QKEY=$(kubectl -n localmind get secret localmind-secrets -o jsonpath='{.data.qdrant-api-key}' | base64 -d)
kubectl port-forward -n localmind svc/qdrant 6334:6333 &
curl -s localhost:6334/collections -H "api-key: $QKEY"
#   empty list until the first page is indexed
```

### 8.5 Redis stream

```bash
kubectl -n localmind exec deploy/redis -- redis-cli XLEN index:events
#   (integer) 0   — rises during the first crawl, returns to ~0 after it drains
```

## 9. Step 7 — first real crawl (onboard a partner)

Partner onboarding is a plain SQL insert — no API, no script
(`adding-new-partners.txt` and `docs/tiering.md` document the contract):

```bash
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler <<'SQL'
INSERT INTO partners (domain) VALUES ('example.com');
SQL
```

All other columns have DB-level `server_default`s; the site's profile and
collection come from `config/profiles.yaml` (fallback `general`) and from a
`config/sites.yaml` entry if one exists for the domain.

What happens next, and where to watch each stage:

```bash
# 1. the feed worker picks up the new partner on its next tick and enqueues the first pages:
kubectl -n localmind logs deploy/localmind-crawler -f | grep -i feed
# 2. the stream rises, then drains:
kubectl -n localmind exec deploy/redis -- watch -n1 redis-cli XLEN index:events
# 3. first indexed page: the indexer downloads the profile's embedding model
#    (BAAI/bge-base-en-v1.5, ~400 MB) — the first crawl is slow by design (gotcha #5):
kubectl -n localmind logs deploy/localmind-indexer -f
# 4. the collection appears, then points accumulate:
curl -s localhost:6334/collections -H "api-key: $QKEY" | python3 -m json.tool
curl -s "localhost:6334/collections/general/points/count" -X POST \
     -H "api-key: $QKEY" -H 'Content-Type: application/json' -d '{"exact":true}'
# 5. DB rows:
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler \
  -c "SELECT count(*) FROM sites; SELECT count(*) FROM page_stats;"
```

The full end-to-end verification (simulated clicks and impressions,
demote→vault→restore, fast-forward tiering via `config/smoke-fast.yaml`) is
`docs/smoke-testing.md` — it runs the processes locally against this same
shared cluster through port-forwards.

## 10. Day-2 operations

### 10.1 Code change (redeploy)

```bash
minikube image build -f crawler/Dockerfile -t registry.example.com/localmind-crawler:latest .   # + indexer/search as changed
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-indexer localmind-search
# :latest never triggers a rollout by itself — the restart is required.
```

### 10.2 Config change (no rebuild)

```bash
# e.g. you edited config/tiers.yaml or config/sites.yaml
kubectl create configmap localmind-crawler-config -n localmind --dry-run=client \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml --from-file=config/tiers.yaml \
  -o yaml | kubectl apply -f -
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-indexer localmind-search
```

### 10.3 Token change

```bash
kubectl -n localmind patch secret localmind-crawler-secret -p '{"stringData":{"admin-token":"NEW"}}'
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-search
```

### 10.4 tierctl against the cluster

`cli/tierctl.py` only needs `DATABASE_URL` — point it at the port-forward:

```bash
export DATABASE_URL="postgresql+psycopg://localmind_crawler:$PGPASS@localhost:5433/localmind_crawler"
python3 cli/tierctl.py status            # heat snapshot across partners
python3 cli/tierctl.py config example.com   # effective tiering config + where each value came from
```

### 10.5 Reporting traffic (what drives tiering)

```bash
curl -s -X POST localhost:8000/hits -H "Authorization: Bearer CHANGEME" \
  -H 'Content-Type: application/json' \
  -d '[{"url":"https://example.com/a","site_domain":"example.com","count":3,"impressions":40}]'
#   {"ok":1}
```

### 10.6 Scaling

The crawler HPA is already applied (1–8, 70% CPU — `FOR UPDATE SKIP LOCKED`
makes multiple replicas safe). The indexer is a consumer-group worker:
`kubectl -n localmind scale deploy/localmind-indexer --replicas=N`, or KEDA
(section 11). Redis is deliberately a single replica.

## 11. Optional: KEDA autoscaling for the indexer

```bash
helm repo add keda https://kedacore.github.io/charts && helm repo update
helm install keda keda/keda --namespace keda --create-namespace
kubectl apply -f k8s/indexer-keda.yaml
```

Scales `localmind-indexer` 1→10 on the stream's undelivered count (lag ≥ 200).
Skip it if you don't have the headroom — the base Deployment is correct at
any fixed replica count.

## 12. Gotchas (symptom → cause → fix)

#1 Crawler pod CrashLoopBackOff; log shows
`sqlalchemy.exc.OperationalError: ... localmind-pg-rw...:5432`
→ the `pg-crawler-db-init` Job never ran (or the cluster was destroyed and
recreated). Run/wait it (section 3, item 2). The secret can exist while the
database behind it does not.

#2 `CreateContainerConfigError: secret "localmind-crawler-secret" not found`
(or `key "admin-token" not found`)
→ the section-4 secret was never created, or under the wrong name. The crawler
and search reference `localmind-crawler-secret` — NOT the search repo's
`localmind-secrets`, which has a different key set.

#3 `CreateContainerConfigError: configmap "localmind-crawler-config" not found`
→ section 4 not run. Note the search repo also creates a ConfigMap named
`localmind-config` in this namespace — they are two different objects; never
merge them.

#4 Indexer pod Pending on a small VM
→ its 3 Gi memory request doesn't fit next to PG + Qdrant + search. Scale
the crawler to 1 replica, or temporarily lower the indexer's requests.

#5 First crawl is very slow; indexer log shows a Hugging Face download
→ normal: the profile's sentence-transformers model downloads inside the pod
on first use (bge-base ~400 MB, bge-small ~100 MB) and caches. If the cluster
has no outbound internet, the first page never completes — give the pods
egress, or pre-pull the models and bake them into the image.

#6 `/web` returns 400 (`no API key configured for provider ...`)
→ by design: the cse/bing keys are empty in `localmind-crawler-secret`.
Fill them (10.3) or switch `WEB_SEARCH_PROVIDER`.

#7 `/hits` or `/admin/*` returns 401
→ `ADMIN_TOKEN` is set but the request lacks `Authorization: Bearer <token>`
(or the token is stale after a 10.3 rotation).

#8 Stream length never drops; no new points in Qdrant
→ indexer down or stuck on the model download (check its log), or a message
failed into the DLQ: `kubectl -n localmind exec deploy/redis -- redis-cli XLEN index:dead`.

#9 Rebuilt the image but the pod still runs old code
→ `:latest` + `IfNotPresent`: kubelet won't re-pull and nothing restarts by
itself. `kubectl rollout restart` (10.1).

#10 Crawler works in k8s but `docs/smoke-testing.md` can't reach Postgres
→ the smoke runbook runs the processes on your machine; from the cluster side
only the port-forward (8.3) + the pg-crawler Job matter. Its Redis is a local
`docker run redis:7`, not the cluster's redis.

#11 Qdrant REST calls return 403
→ the shared release has an API key: every call needs
`-H "api-key: $QKEY"`.

#12 After scaling the indexer, `XINFO GROUPS index:events indexers` still
shows a pending count
→ the consumer group is shared by design across replicas. Pending only drains
as consumers ACK (the indexer ACKs after upserting).

## 13. Glossary

* **Profile** — a site category with its own embedding model, vector size and
  chunking (`config/profiles.yaml`); decides the Qdrant collection. Profiles
  sharing a collection must share model + size (validated at startup).
* **Heat** — `decayed_views + decayed_impressions`, exponential decay over
  `half_life_days` (`shared/tiers.py`); decides the tier.
* **Tier** — t0 full / t1 summary / t2 stub / t3 vault (no vectors; text in
  `page_archives`, findable by keyword only).
* **`index:events` / `index:dead`** — Redis stream work queue / dead-letter,
  consumer group `indexers`; message types: page, close, move, demote,
  promote (`shared/messages.py`).
* **Demote / promote** — tier-bot moves between tiers; demote-to-t3 writes the
  archive and deletes vectors; promote re-embeds.
* **Restore** — `/restore` on the search app: re-embed a vaulted page from
  `page_archives` without a live fetch.

## 14. Production notes (later)

* Replace the `registry.example.com/...:latest` placeholders with a real
  registry and immutable tags (`IfNotPresent` stays harmless).
* Prod Postgres secret template lives in the ai-web-search repo
  (`secrets.prod.template.yaml`); the Barman block in its `cngp.yaml` backs
  up the whole cluster — `localmind_crawler` included — with one setup.
  Off-cluster backup for Postgres is an open item (see `todo.txt`).
* HPA + KEDA objects ship in this repo and are what you want on a real
  cluster. Swap the dev-grade `redis.yaml` for a persistent Redis/Valkey,
  keeping the service name `redis` and port 6379.
* Qdrant HA shape = the search repo's `qdrant-values.yaml` (3 replicas);
  `qdrant-values.local.yaml` is the minikube shape.