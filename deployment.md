# LocalMind — unified deployment (both repos, minikube)

Single step-by-step reference for a **fresh** local deploy of the whole
**LocalMind** system — the ai-web-search "server" **and** the ai-web-crawler
stack — in the **correct sequential order**.

This doc **supersedes** the two split references:
- the old crawler-only `ai-web-crawler/deployment.md` (now consolidated here), and
- `ai-web-search/dev-env/ai-gen-deployment-steps` (server-only; banner added).

Everything runs in **one namespace: `localmind`** (a deliberate decision — see
Section 0). The deploy is **four phases**; do them in order and do not skip the
wait/verify gates — each later phase depends on the one before it.

```
PHASE 0  shared infra   (ai-web-search repo)   one-time
PHASE 1  server app     (ai-web-search repo)   frontend + backend + ingress
PHASE 2  crawler stack  (ai-web-crawler repo)  crawler + indexer + search
PHASE 3  first crawl    (ai-web-crawler repo)  onboard a partner, watch it flow
```

> **Repo locations** (siblings under `~/git/`):
> - `~/git/ai-web-search` — the server + shared infra (namespace, Postgres,
>   Qdrant, secrets, frontend/backend/ingress).
> - `~/git/ai-web-crawler` — the pipeline (crawler, indexer, search-slice,
>   Redis, tierctl).
> Every command below is labelled with which repo you are in.

---

## 0. How the two repos fit together

The crawler is **its own service**. It deliberately shares **exactly two**
things with the server, both deployed by the ai-web-search repo in PHASE 0:

1. **The Postgres cluster** (`localmind-pg`, CNPG). The server owns database
   `localmind` (users, subscriptions). The crawler gets its **own** database
   `localmind_crawler` and its **own** login role `localmind_crawler` inside
   the *same* cluster, created **operator-managed** by `crawler-db.yaml`
   (a `DatabaseRole` + `Database`; the CNPG operator performs the DDL as the
   true superuser — no Job, no psql, no privileged credentials). Same cluster
   = one HA setup, one backup, one monitoring surface; separate database =
   clean ownership + least privilege (neither app can touch the other's DB).
2. **Qdrant** (Helm release, svc `qdrant:6333`). The crawler's indexer
   **writes** per-profile collections (`general`, `docs`, `encyclopedia`,
   `news` — see `ai-web-crawler/config/profiles.yaml`); the server **reads**
   them for vector search.

Everything else is owned by exactly one repo. **Deploy order is fixed:
server (PHASE 0+1) first, crawler (PHASE 2) after** — the crawler's pre-check
in PHASE 2 verifies the shared resources exist before it applies anything.

> **Why one namespace?** `localmind` is a *logical* scope (RBAC, quotas,
> ownership), not a security boundary. Both apps share the same Postgres,
> Qdrant and Redis, and there is a single owner/team and one deploy lifecycle,
> so splitting namespaces would add wiring without buying isolation. Hardening
> (NetworkPolicy on the data layer, egress control on the crawler, RBAC on a
> scoped CI service account) is an add-on — see Section 11 — not a reason to
> re-namespace.

```
 ai-web-search repo (shared — deployed FIRST, PHASE 0+1)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ namespace localmind                                                  │
 │ localmind-pg  (CNPG cluster)                                         │
 │   ├─ db localmind          (server app: users, subscriptions)        │
 │   └─ db localmind_crawler  (role+db: crawler-db.yaml, op-managed)    │
 │ qdrant        (Helm release, svc qdrant:6333)                        │
 │ secrets: localmind-secrets, localmind-crawler-pg, -cred, qdrant-...  │
 │ localmind-backend (:8080)  localmind-frontend (:80)  ingress         │
 └──────────────────────────────────────────────────────────────────────┘
      ▲ upserts vectors per collection      ▲ connects
      │                                     │
 ai-web-crawler repo (THIS doc, PHASE 2+3)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ localmind-crawler ─XADD─► redis ─consumer group─► localmind-indexer  │
 │  (API + crawl workers, index:events (embeds chunks, upserts to       │
 │   feed worker, tier bot) index:dead Qdrant)                          │
 │ localmind-search ── same DB + same redis (keyword leg, /restore,     │
 │  /web; /restore republishes to the indexer)                          │
 └──────────────────────────────────────────────────────────────────────┘
```

---

## 1. Prerequisites (one-time, host)

- `kubectl`, `helm`, `minikube` on PATH. Python 3.11+ for the host helper
  scripts (`ingest.py`, `seed_test_user.py`, `sync_stripe_prices.py`).
  Docker is **not** required — images are built with `minikube image build`
  (see Gotchas S-2/S-3).
- **Current checkouts of BOTH repos.** A stale ai-web-search checkout predates
  `crawler-db.yaml` + the `localmind-crawler-cred` secret and instead ships the
  deprecated `pg-crawler.yaml` Job, which dies with `permission denied to
  create role` (Gotcha C-14). Sync both (`git pull`) before starting.
- minikube sized for the load: **≥ 4 CPU / ≥ 11 GiB RAM**. The indexer image
  pulls CPU torch and requests 3 GiB; a full HA build OOMs a small VM
  (Gotcha X-2). This doc uses the **local** override manifests
  (`cngp.local.yaml`, `qdrant-values.local.yaml`) + single replicas.
- Outbound internet from the cluster: the first crawl downloads the
  sentence-transformers embedding models from Hugging Face (Gotcha C-5).

```bash
minikube start --cpus=4 --memory=12288     # or your existing start
minikube addons enable ingress
minikube status
```

---

## 2. Object ownership (who owns what, namespace `localmind`)

Two objects with the same name in one namespace are the **same** object — the
last apply wins and the other app silently breaks. So each repo owns
disjoint names. This is the rule that keeps the two-repo split safe.

| Object | Owner | Manifest | Notes |
|---|---|---|---|
| Namespace `localmind` | ai-web-search | `namespace.yaml` | shared |
| CNPG operator | ai-web-search | helm, `cnpg-system` | once per cluster |
| `localmind-pg` (CNPG cluster) | ai-web-search | `cngp.local.yaml` (local) / `cngp.yaml` (prod) | shared Postgres |
| `DatabaseRole` + `Database` (`localmind_crawler`) | ai-web-search | `crawler-db.yaml` | **operator-managed** |
| `qdrant-secret` | ai-web-search | `kubectl create` (not git) | Qdrant API key |
| Qdrant Helm release + svc `qdrant` | ai-web-search | helm + `qdrant-values.local.yaml` | shared vector DB |
| `qdrant-external` (LoadBalancer) | ai-web-search | `qdrant-external.yaml` | optional, host-side tools |
| `localmind-secrets` | ai-web-search | `secrets.dev.yaml` / `secrets.prod.template.yaml` | qdrant key, Stripe, JWT, … |
| `localmind-crawler-pg` | ai-web-search | `secrets.*.yaml` | crawler `DATABASE_URL` |
| `localmind-crawler-cred` | ai-web-search | `secrets.*.yaml` | op-synced role password |
| `localmind-pg-app` | CNPG (auto) | — | server DB owner creds |
| `localmind-config` ConfigMap | ai-web-search | `configmap.yaml` | server env — **don't reuse the name** |
| `localmind-backend` / `localmind-frontend` (+svc/HPA/PDB) | ai-web-search | `backend-deployment.yaml`, `frontend-deployment.yaml` | server app |
| `localmind-frontend-ingress` / `localmind-backend-ingress` | ai-web-search | `ingress.yaml` | `/*` and `/api/*` |
| `localmind-crawler-rbac` Role/RoleBinding/SA (optional) | ai-web-search | `crawler-rbac.yaml` | only if deploy identity ≠ cluster-admin |
| `localmind-crawler-config` ConfigMap | ai-web-crawler | `k8s/configmap.md` | profiles/sites/tiers |
| `localmind-crawler-secret` Secret | ai-web-crawler | `k8s/configmap.md` | admin-token, anthropic, cse/bing |
| `redis` Deployment+Service | ai-web-crawler | `k8s/redis.yaml` | message bus (dev-grade) |
| `localmind-crawler` Deployment+Service+HPA | ai-web-crawler | `k8s/crawler.yaml` | API + workers |
| `localmind-indexer` Deployment (+KEDA) | ai-web-crawler | `k8s/indexer.yaml`, `k8s/indexer-keda.yaml` | embed + upsert |
| `localmind-search` Deployment+Service | ai-web-crawler | `k8s/search.yaml` | keyword/restore/web slice |
| crawler/indexer/search images | ai-web-crawler | `*/Dockerfile` | built into minikube |

**Do NOT apply** the legacy monolithic server manifests — `k8s-split.yaml`,
`service.yaml`, `hpa.yaml`, `pdb.yaml`, and the monolithic `Dockerfile` all
target a deployment named `localmind` that does not exist in the split
frontend/backend setup (Gotcha S-1).

---

## 3. Secret inventory

| Secret | Keys | Created by | Value source |
|---|---|---|---|
| `qdrant-secret` | `api-key` | PHASE 0, `kubectl create` | must equal `localmind-secrets/qdrant-api-key` |
| `localmind-secrets` | `qdrant-api-key`, `stripe-secret-key`, `stripe-webhook-secret`, `jwt-secret`, `admin-api-key`, `SMTP_PASSWORD` | PHASE 0, `secrets.dev.yaml` | dev values (prod: `secrets.prod.template.yaml` + GitHub secrets) |
| `localmind-pg-app` | `user`,`password`,`dbname`,`host`,`port`,`uri` | **CNPG auto** after cluster Ready | CNPG-generated; server DB owner `localmind_app` |
| `localmind-crawler-pg` | `user`,`password`,`dbname`,`host`,`port`,`database-url` | PHASE 0, `secrets.dev.yaml` | **one** password, used by crawler pods (`DATABASE_URL`) |
| `localmind-crawler-cred` | `username`,`password` (basic-auth) | PHASE 0, `secrets.dev.yaml` | **must equal** `localmind-crawler-pg.password`; the operator syncs the PG role to it (`cnpg.io/reload: "true"`) |
| `localmind-crawler-secret` | `admin-token`,`anthropic-key`,`cse-api-key`,`cse-cx`,`bing-api-key` | PHASE 2, `k8s/configmap.md` | tokens; empty = feature off |

**The one-password invariant** (Gotchas C-1/C-3): `localmind-crawler-pg.password`
**and** `localmind-crawler-cred.password` must be the **same value** — the
operator makes the `localmind_crawler` role's password match `-cred`, while the
pods connect with `-pg`'s `database-url`. If they drift, pods crashloop with
`password authentication failed for user "localmind_crawler"`. Rotate by
changing **both** (the `-cred` label applies it live); never hand `ALTER ROLE`.

---

## 4. PHASE 0 — one-time shared infra  (repo: `ai-web-search`)

All manifest paths below are under `~/git/ai-web-search/dev-env/k8s-manifests/`.
Run from `~/git/ai-web-search/dev-env`.

```bash
cd ~/git/ai-web-search/dev-env
M=k8s-manifests
```

### 0.1 Namespace

```bash
kubectl apply -f $M/namespace.yaml
```

### 0.2 CNPG operator (once per cluster)

```bash
helm repo add cnpg https://cloudnative-pg.github.io/charts && helm repo update
helm upgrade --install cnpg cnpg/cloudnative-pg \
  --namespace cnpg-system --create-namespace
kubectl get pods -n cnpg-system            # controller up before 0.4
```
> **Backups (skip for local).** The prod `cngp.yaml` uses the Barman Cloud
> Plugin (needs cert-manager + `cnpg/plugin-barman-cloud`). Local
> `cngp.local.yaml` has no backup blocks — skip all of this for a local
> deploy. On a real cluster, install cert-manager + the plugin here first.

### 0.3 Qdrant (vector DB)

```bash
helm repo add qdrant https://qdrant.github.io/qdrant-helm && helm repo update
kubectl create secret generic qdrant-secret \
  --namespace localmind --from-literal=api-key=local-testing-key
helm upgrade --install qdrant qdrant/qdrant \
  --namespace localmind \
  --values $M/qdrant-values.local.yaml \
  --set config.service.api_key="$(kubectl -n localmind get secret qdrant-secret -o jsonpath='{.data.api-key}' | base64 -d)"
kubectl -n localmind get pods -l app.kubernetes.io/name=qdrant   # 1 replica, Running
```
Local uses `qdrant-values.local.yaml` (replicaCount 1, standalone). The key in
`qdrant-secret` must equal `localmind-secrets/qdrant-api-key` (next step) —
both are `local-testing-key` in dev.

### 0.4 App + crawler shared secrets

```bash
kubectl apply -f $M/secrets.dev.yaml        # localmind-secrets, localmind-crawler-pg, localmind-crawler-cred
# confirm the two crawler PG secrets carry the SAME password (Section 3):
kubectl -n localmind get secret localmind-crawler-pg   -o jsonpath='{.data.password}' | base64 -d; echo
kubectl -n localmind get secret localmind-crawler-cred -o jsonpath='{.data.password}' | base64 -d; echo
```
For prod, render `$M/secrets.prod.template.yaml` from GitHub secrets instead
(see Section 11).

### 0.5 CNPG cluster (the shared Postgres)

```bash
kubectl apply -f $M/cngp.local.yaml          # local: 1 instance, no Barman
kubectl -n localmind wait --for=condition=Ready cluster/localmind-pg --timeout=300s
kubectl -n localmind get secret localmind-pg-app      # CNPG auto-created; server DB owner
```

### 0.6 Crawler role + database (operator-managed — **the permission fix**)

```bash
kubectl apply -f $M/crawler-db.yaml          # DatabaseRole + Database
# poll ~15s — the operator reconciles; both must be Reconciled:
kubectl -n localmind get databaseroles       # → localmind-crawler-role ... Reconciled
kubectl -n localmind get databases           # → localmind-crawler-db ... Reconciled
```
This is the mechanism that replaces the old `pg-crawler.yaml` Job. The operator
creates role + db `localmind_crawler` and syncs its password to
`localmind-crawler-cred`. On an existing cluster where the role/db were created
manually, applying **adopts** them (idempotent). **If either is not
`Reconciled`, stop** — the crawler in PHASE 2 will crashloop (Gotcha C-1).

### 0.7 (Optional) crawler deploy RBAC

Only needed if the identity that runs PHASE 2 is **not** cluster-admin (e.g. a
scoped CI ServiceAccount). On local minikube you deploy as the `minikube`
admin — skip this.

```bash
# kubectl apply -f $M/crawler-rbac.yaml     # set the RoleBinding subjects first
```

**PHASE 0 complete when:** `localmind-pg` is Ready, `qdrant` is Running, and
`databaseroles`/`databases` are both `Reconciled`.

---

## 5. PHASE 1 — server app  (repo: `ai-web-search`)

Still in `~/git/ai-web-search/dev-env`.

### 5.1 Build the server images (into minikube)

```bash
minikube image build -f Dockerfile.backend  -t your-registry/localmind-backend:latest  .
minikube image build -f Dockerfile.frontend -t your-registry/localmind-frontend:latest .
minikube image ls | grep localmind          # both present (Gotcha S-3: don't trust a piped exit code)
```
`Dockerfile.backend` installs **CPU-only torch first** — do not "simplify" it
back to a plain `pip install`, or the build OOMs on a small VM (Gotcha S-2).

### 5.2 Apply config + deployments + ingress

```bash
kubectl apply -f $M/configmap.yaml               # localmind-config (server env)
kubectl apply -f $M/backend-deployment.yaml      # Deployment + svc + HPA + PDB
kubectl apply -f $M/frontend-deployment.yaml     # Deployment + svc + HPA + PDB
minikube addons enable ingress                  # (already enabled in Section 1, harmless)
kubectl apply -f $M/ingress.yaml                # localmind-frontend-ingress (/*) + localmind-backend-ingress (/api/*)

# small VM: the manifests ship replicas:2; drop to 1 (Gotcha X-2)
kubectl -n localmind scale deployment/localmind-backend  --replicas=1
kubectl -n localmind scale deployment/localmind-frontend --replicas=1
```
(`STRIPE_PRICE_MAP` is intentionally NOT in `configmap.yaml` —
`sync_stripe_prices.py --k8s` patches it in at runtime; adding it to the file
would let a later `apply` clobber it. Gotcha S-5.)

### 5.3 Verify the server is healthy

```bash
kubectl -n localmind get pods,deploy,svc
# expose the app to your browser (this exact URL matters — Gotcha S-4):
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80 &
curl -s localhost:8080/healthz                 # {"status":"ok"} (or the frontend page)
```
Optional host-side seeding (needs Qdrant + backend exposed):
```bash
kubectl -n localmind port-forward svc/qdrant 6333:6333 &
kubectl -n localmind port-forward svc/backend-service 8080:8080 &
export QDRANT_URL="http://localhost:6333" QDRANT_API_KEY="local-testing-key"
ADMIN_KEY=$(kubectl -n localmind get secret localmind-secrets -o jsonpath='{.data.admin-api-key}' | base64 -d)
python3 seed_test_user.py --url http://localhost:8080/api --admin-key "$ADMIN_KEY" --email test@yourdomain.com
python3 ingest.py --collection test --dir html-files        # optional: seed content
```

**PHASE 1 complete when:** `localmind-backend` and `localmind-frontend` are
`Ready`, and `curl localhost:8080/healthz` answers.

---

## 6. PHASE 2 — crawler stack  (repo: `ai-web-crawler`)

`cd ~/git/ai-web-crawler`. Manifests under `k8s/`.

### 6.1 Pre-check the shared resources exist (read-only)

Everything here is owned by PHASE 0. Every line must hold before you apply
anything from this repo; if one is red, finish PHASE 0 first.

```bash
kubectl -n localmind get cluster localmind-pg                     # Ready
kubectl -n localmind get databaseroles                            # localmind-crawler-role Reconciled
kubectl -n localmind get databases                                # localmind-crawler-db Reconciled
kubectl -n localmind get secret localmind-crawler-pg localmind-crawler-cred localmind-secrets
kubectl -n localmind get svc qdrant
# crawler PG password invariance (Section 3):
kubectl -n localmind get secret localmind-crawler-pg   -o jsonpath='{.data.password}' | base64 -d; echo
kubectl -n localmind get secret localmind-crawler-cred -o jsonpath='{.data.password}' | base64 -d; echo
```

### 6.2 Crawler config + token secret (idempotent apply)

Use `--dry-run=client | apply` so re-runs don't fail with `AlreadyExists`
(a plain `kubectl create` does — this is what makes the deploy repeatable /
CI-ready).

```bash
kubectl create configmap localmind-crawler-config --namespace localmind \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml --from-file=config/tiers.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# empty token values are valid and mean "feature off":
kubectl create secret generic localmind-crawler-secret --namespace localmind \
  --from-literal=admin-token=CHANGEME \
  --from-literal=anthropic-key= \
  --from-literal=cse-api-key= \
  --from-literal=cse-cx= \
  --from-literal=bing-api-key= \
  --dry-run=client -o yaml | kubectl apply -f -
```
(`localmind-crawler-secret` is **not** the server's `localmind-secrets` —
different key set, different owner. Gotcha C-2.)

### 6.3 Redis (the message bus)

```bash
kubectl apply -f k8s/redis.yaml
kubectl -n localmind rollout status deploy/redis --timeout=90s
```

### 6.4 Build the crawler images

```bash
minikube image build -f crawler/Dockerfile -t registry.example.com/localmind-crawler:latest .   # Playwright base; largest (~2–3 GiB)
minikube image build -f indexer/Dockerfile -t registry.example.com/localmind-indexer:latest .   # CPU torch; slowest
minikube image build -f search/Dockerfile  -t registry.example.com/localmind-search:latest .    # quick
minikube image ls | grep localmind
```
`registry.example.com/...:latest` are placeholders — no real registry locally;
every pod uses `imagePullPolicy: IfNotPresent`, so kubelet uses minikube's store.

### 6.5 Apply the crawler stack

```bash
kubectl apply -f k8s/crawler.yaml    # Deployment + Service + HPA (1–8)
kubectl apply -f k8s/indexer.yaml    # Deployment (consumer group; 1 replica fine for dev)
kubectl apply -f k8s/search.yaml     # Deployment + Service
# kubectl apply -f k8s/indexer-keda.yaml    # OPTIONAL — only if KEDA is installed (Section 9.7)

kubectl -n localmind rollout status deploy/localmind-crawler --timeout=180s
kubectl -n localmind rollout status deploy/localmind-indexer  --timeout=300s
kubectl -n localmind rollout status deploy/localmind-search   --timeout=120s
```
Small VM: if crawler pods sit Pending, `kubectl -n localmind scale deploy/localmind-crawler --replicas=1`.

### 6.6 Verify the crawler stack is healthy

```bash
kubectl -n localmind get pods -l 'app in (redis,localmind-crawler,localmind-indexer,localmind-search)'

# crawler API
kubectl port-forward -n localmind svc/localmind-crawler 8000:80 &
curl -s localhost:8000/healthz                                            # {"ok":true}
curl -s -X POST localhost:8000/admin/sync-manual -H "Authorization: Bearer CHANGEME"   # {"synced":0}

# search slice
kubectl port-forward -n localmind svc/localmind-search 8001:80 &
curl -s localhost:8001/health                                             # {"ok":true}
curl -s "localhost:8001/search?q=test"                                    # {"results":[]} until first crawl

# Postgres (crawler's DB — tables created by init_db() on first start)
kubectl port-forward -n localmind svc/localmind-pg-rw 5433:5432 &
PGPASS=$(kubectl -n localmind get secret localmind-crawler-pg -o jsonpath='{.data.password}' | base64 -d)
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler -c "\dt"

# Qdrant + Redis stream
QKEY=$(kubectl -n localmind get secret localmind-secrets -o jsonpath='{.data.qdrant-api-key}' | base64 -d)
kubectl port-forward -n localmind svc/qdrant 6334:6333 &
curl -s localhost:6334/collections -H "api-key: ***"                      # [] until first crawl
kubectl -n localmind exec deploy/redis -- redis-cli XLEN index:events     # (integer) 0
```

**PHASE 2 complete when:** all four Deployments are `Ready`, both `/healthz`
and `/health` answer, and the crawler's five tables exist in Postgres.

---

## 7. PHASE 3 — first real crawl  (repo: `ai-web-crawler`)

Partner onboarding is a plain SQL insert — there is no API or script for it
(the contract is documented in `adding-new-partners.txt` and `docs/tiering.md`).

```bash
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler <<'SQL'
INSERT INTO partners (domain) VALUES ('example.com');
SQL
```
All other columns have DB `server_default`s; the profile + collection come from
`config/profiles.yaml` (fallback `general`) and a `config/sites.yaml` entry if
one exists for the domain.

Watch each stage:
```bash
# 1. feed worker picks up the partner on its next tick and enqueues pages:
kubectl -n localmind logs deploy/localmind-crawler -f | grep -i feed
# 2. stream rises, then drains:
kubectl -n localmind exec deploy/redis -- watch -n1 redis-cli XLEN index:events
# 3. first indexed page: the indexer downloads the embedding model (~400 MB) —
#    the first crawl is SLOW by design (Gotcha C-5):
kubectl -n localmind logs deploy/localmind-indexer -f
# 4. collection appears, then points accumulate:
curl -s localhost:6334/collections -H "api-key: ***" | python3 -m json.tool
curl -s localhost:6334/collections/general/points/count -X POST -H "api-key: ***" -H 'Content-Type: application/json' -d '{"exact":true}'
# 5. DB rows:
PGPASSWORD="***" psql -h localhost -p 5433 -U localmind_crawler -d localmind_crawler \
  -c "SELECT count(*) FROM sites; SELECT count(*) FROM page_stats;"
```
The full end-to-end lifecycle (simulated hits/impressions, demote→vault→restore
on a fast-forward `config/smoke-fast.yaml`) is `docs/smoke-testing.md`.

---

## 8. End-to-end verification checklist

Run this once, top to bottom, to confirm the whole system is up:

```bash
kubectl -n localmind get cluster localmind-pg        # Ready
kubectl -n localmind get databaseroles               # Reconciled
kubectl -n localmind get databases                   # Reconciled
kubectl -n localmind get pods -l 'app.kubernetes.io/name=qdrant' -o jsonpath='{.items[*].status.phase}'
kubectl -n localmind get pods                        # all Running/Ready (no CrashLoop/Pending)
curl -s localhost:8080/healthz                       # server
curl -s localhost:8000/healthz                       # crawler
curl -s localhost:8001/health                        # search slice
```
All green = the deploy is complete.

---

## 9. Day-2 operations

### 9.1 Code change (redeploy)
```bash
# rebuild the changed image(s), then restart (Gotcha X-3: :latest + IfNotPresent
# never re-pulls, so the restart is required)
minikube image build -f crawler/Dockerfile -t registry.example.com/localmind-crawler:latest .
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-indexer localmind-search
```
(Server: rebuild `Dockerfile.backend`/`-frontend` the same way, then
`rollout restart deployment/localmind-backend localmind-frontend`.)

### 9.2 Config change (no rebuild)
```bash
# e.g. you edited config/tiers.yaml or config/sites.yaml
kubectl create configmap localmind-crawler-config -n localmind --dry-run=client \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml --from-file=config/tiers.yaml \
  -o yaml | kubectl apply -f -
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-indexer localmind-search
```

### 9.3 Token change
```bash
kubectl -n localmind patch secret localmind-crawler-secret -p '{"stringData":{"admin-token":"NEW"}}'
kubectl -n localmind rollout restart deploy/localmind-crawler localmind-search
```

### 9.4 tierctl against the cluster
`cli/tierctl.py` only needs `DATABASE_URL` — point it at the port-forward:
```bash
export DATABASE_URL="postgresql+psycopg://localmind_crawler:$PGPASS@localhost:5433/localmind_crawler"
python3 cli/tierctl.py status               # heat snapshot across partners
python3 cli/tierctl.py config example.com   # effective tiering config + provenance
python3 cli/tierctl.py dry-run              # planned transitions, applies nothing
```

### 9.5 Reporting traffic (what drives tiering)
```bash
curl -s -X POST localhost:8000/hits -H "Authorization: Bearer CHANGEME" -H 'Content-Type: application/json' \
  -d '[{"url":"https://example.com/a","site_domain":"example.com","count":3,"impressions":40}]'   # {"ok":1}
```

### 9.6 Scaling
Crawler HPA is already applied (1–8, 70% CPU — `FOR UPDATE SKIP LOCKED` makes
multiple replicas safe). Indexer is a consumer-group worker:
`kubectl -n localmind scale deploy/localmind-indexer --replicas=N`. Redis is a
single replica by design.

### 9.7 Optional: KEDA autoscaling for the indexer
```bash
helm repo add keda https://kedacore.github.io/charts && helm repo update
helm install keda keda/keda --namespace keda --create-namespace
kubectl apply -f k8s/indexer-keda.yaml      # scales indexer 1→10 on stream lag ≥ 200
```

---

## 10. Gotchas (symptom → cause → fix)

### Shared infra
**X-1 storageClass is hardcoded `standard`.** Correct on minikube (its default
hostpath class is literally `standard`) but wrong on the clouds — PVCs sit
Pending. Swap per provider: GKE `pd-ssd`, AWS `gp3`, Azure `managed-premium`.

**X-2 Replica counts are tuned for HA, not a small VM.** Prod defaults
(qdrant 3, CNPG 3, backend/frontend 2) won't schedule on 11 GiB. For local use
`cngp.local.yaml` + `qdrant-values.local.yaml` and scale the two deployments to
1 (Sections 5.2, 9.6).

**X-3 `:latest` + `imagePullPolicy: IfNotPresent`.** minikube will NOT re-pull
on rebuild — pods keep the stale image until you `minikube image build` the new
one AND `rollout restart` (Section 9.1).

### Server (ai-web-search)
**S-1 Legacy monolithic manifests.** `k8s-split.yaml`, `service.yaml`,
`hpa.yaml`, `pdb.yaml`, and the monolithic `Dockerfile` all target a deployment
named `localmind` that does not exist in the split setup. `apply -f hpa.yaml`
*succeeds* but creates an inert HPA; the PDB/service match nothing;
`k8s-split.yaml` also still probes `/collections` (needs a JWT now → pods never
Ready) and does internal cert-manager TLS (a different approach). **Do not
apply them** — HPA+PDB come from the two deployment files.

**S-2 Backend build OOM / SIGSEGV (139) / "hash mismatch".** A plain
`pip install -r requirements.txt` pulls default PyPI torch (~3 GB of nvidia CUDA
wheels); on a 0-swap VM that OOMs the build. `Dockerfile.backend` already
installs CPU-only torch first (`--index-url .../whl/cpu torch`) — keep it.

**S-3 `build | tail` reports the wrong exit code** (the pipeline returns tail's
0). Check the log, or `minikube image build ... > /tmp/build.log 2>&1; echo $?`.

**S-4 `FRONTEND_URL` must equal the exact browser URL** (scheme+host+port, no
trailing slash). Stripe redirects and reset links use it. Local: keep
`http://localhost:8080` to match the 8080:80 port-forward.

**S-5 `STRIPE_PRICE_MAP` is deliberately NOT in `configmap.yaml`** —
`sync_stripe_prices.py --k8s` patches it in; listing it would let a later
`apply` overwrite the value.

**S-8 `/collections` requires a JWT now → it can't be a pod probe.** The split
`backend-deployment.yaml` uses `/healthz` (correct).

**S-9 CNPG's auto `uri` uses `postgresql://`; SQLAlchemy needs `+psycopg2`.**
The backend assembles `DATABASE_URL` from `PG*` env vars at startup — don't
"fix" it back to the secret's `uri` key.

**S-11 `qdrant-external` is a LoadBalancer** with an AWS-specific
source-range annotation (a no-op off-AWS, still exposes an external IP). Only
apply it where host-side tools (ingest.py) need it; lock the source range on AWS.

**S-12 `ssl-redirect: "false"` on the split ingress** because TLS terminates at
the upstream ALB (traffic reaches the in-cluster ingress as plain HTTP).
Enabling an internal redirect here creates a loop against the ALB.

### Crawler (ai-web-crawler)
**C-1 Crawler pod CrashLoop: `password authentication failed for user
"localmind_crawler"`.** The role/db are missing (fresh cluster where
`crawler-db.yaml` was never applied / cluster recreated) **or** the two
credential secrets disagree (C-3). Check `get databaseroles` and `get databases`; apply
`crawler-db.yaml` if not Reconciled; compare the two secret passwords.

**C-2 `CreateContainerConfigError: secret "localmind-crawler-secret" not found`
(or `key "admin-token" not found`).** The 6.2 secret was never created, or under
the wrong name. The crawler/search reference `localmind-crawler-secret` — NOT
the server's `localmind-secrets`. Same symptom with `localmind-crawler-pg not
found` → the server's `secrets.dev.yaml` predates that secret, or only its first
document was applied.

**C-3 Credential secrets disagree → pods crashloop even when
`get databaseroles` says Reconciled.** The operator syncs the PG role's
password from `localmind-crawler-cred` every reconciliation, while pods connect
with `localmind-crawler-pg`'s `database-url`. Compare both; make them equal,
then `rollout restart` the crawler + search. **Never** hand `ALTER ROLE` — the
operator flips it back next reconciliation.

**C-4 `CreateContainerConfigError: configmap "localmind-crawler-config"
not found`.** Section 6.2 not run. Note the server also has a `localmind-config`
ConfigMap in this namespace — two different objects; never merge them.

**C-5 Indexer Pending / first crawl very slow.** Pending on a small VM → its 3
GiB request doesn't fit (scale crawler to 1). Slow first crawl → the profile's
embedding model (bge-base ~400 MB) downloads in-pod on first use; needs outbound
internet, or pre-bake the models into the image.

**C-6 Rebuilt the image but the pod still runs old code.** `:latest` +
`IfNotPresent` (X-3). `rollout restart` (9.1).

**C-7 `/web` returns 400 (`no API key configured`).** By design — the cse/bing
keys are empty in `localmind-crawler-secret`. Fill them (9.3) or set
`WEB_SEARCH_PROVIDER`.

**C-8 `/hits` or `/admin/*` returns 401.** `ADMIN_TOKEN` is set but the request
lacks `Authorization: Bearer *** (or the token is stale after a 9.3 rotation).

**C-9 Stream length never drops; no new points in Qdrant.** Indexer down / stuck
on the model download (check its log), or a message failed into the DLQ:
`redis-cli XLEN index:dead`.

**C-10 `docs/smoke-testing.md` can't reach Postgres from the cluster.** That
runbook runs the processes on your machine and uses a *local* `docker run
redis:7`, not the cluster's redis; from the cluster side only the port-forward
(6.6) matters.

**C-11 Qdrant REST calls return 403.** The release has an API key — every call
needs `-H "api-key: $QKEY"`.

**C-12 After scaling the indexer, `XINFO GROUPS index:events indexers` still
shows a pending count.** The consumer group is shared by design across
replicas; pending drains only as consumers ACK (the indexer ACKs after upsert).

**C-14 `get databaseroles`/`databases` empty right after a server deploy.** The
ai-web-search checkout on the deploy machine is **stale** — old checkouts
predated `crawler-db.yaml` and shipped the `pg-crawler.yaml` Job instead, which
died with `permission denied to create role` on clusters where the
`localmind-pg-app` user is not a superuser (the reason it was replaced). Its
sibling symptom is the missing `localmind-crawler-cred` secret (C-2). Fix: sync
the checkout (`git pull`), re-verify, then `kubectl apply -f crawler-db.yaml`
and confirm both Reconciled. **This is the historical cause of the recurring
CNPG permission errors — resolved by PHASE 0.6 + current checkouts.**

---

## 11. Cloud / CI (shelved — notes for later)

This doc is the **local minikube** path. When it becomes a GitHub Actions
pipeline, the mapping is:

- **Phase 0 → an `infra` job** (run once / on change): helm installs are
  idempotent (`helm upgrade --install`). **Never `kubectl delete` the Postgres
  cluster in a deploy job** — apply app objects idempotently; a Postgres reset
  is a separate, explicit, destructive step.
- **Phase 1+2 → a `deploy` job** per repo (or one orchestrating workflow):
  build+push images to a real registry tagged with the git short-sha, then
  `kubectl apply` + `kubectl set image ... <img>:<sha>`. Replace the
  `:latest`/`IfNotPresent` placeholders and the `standard` storageClass (X-1).
- **Secrets:** sensitive values → **GitHub secrets** (`${{ secrets.X }}`),
  rendered into `secrets.prod.template.yaml` at deploy time (the template
  already uses `${VAR}` placeholders and feeds **one** `CRAWLER_PG_PASSWORD`
  into both `localmind-crawler-pg` and `localmind-crawler-cred`). Non-sensitive
  per-env values (registry, image tag, ingress host, storageClass,
  `OLLAMA_URL`) → GitHub **environment variables**.
- **RBAC:** if the CI deploy identity is a scoped ServiceAccount (not
  cluster-admin), apply `crawler-rbac.yaml` (PHASE 0.7) with that SA as the
  RoleBinding subject. The operator-managed `crawler-db.yaml` does the DDL —
  the CI identity only needs k8s rights to create the CNPG CRs.
- **Prod Postgres:** use `cngp.yaml` (3 instances + Barman backup blocks) with
  a real S3 bucket; `localmind_crawler` is covered by the same cluster backup.
  Off-cluster backup for Postgres is still an open item (see
  `ai-web-crawler/todo.txt`).

---

## 12. Glossary

- **Profile** — a site category with its own embedding model, vector size and
  chunking (`config/profiles.yaml`); decides the Qdrant collection. Profiles
  sharing a collection must share model + size (validated at startup).
- **Heat** — decayed views + impressions, exponential decay over
  `half_life_days` (`shared/tiers.py`); decides the tier.
- **Tier** — t0 full / t1 summary / t2 stub / t3 vault (no vectors; text in
  `page_archives`, findable by keyword only).
- **`index:events` / `index:dead`** — Redis stream work queue / dead-letter,
  consumer group `indexers`; message types page/close/move/demote/promote
  (`shared/messages.py`).
- **Demote / promote** — tier-bot moves between tiers; demote-to-t3 writes the
  archive then deletes vectors; promote re-embeds.
- **Restore** — `/restore` on the search app: re-embed a vaulted page from
  `page_archives` without a live fetch.
