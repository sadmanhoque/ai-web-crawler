# Crawler config — same namespace as the ai-web-search deployment (localmind).

# ConfigMap name is localmind-crawler-config on purpose: localmind-config
# already belongs to the ai-web-search repo in this namespace, and two
# objects of the same name would clobber each other.
# tiers.yaml is mounted too: the pods read it as /config/tiers.yaml
# (TIERS_FILE), so tiering parameters change with kubectl + restart,
# no image rebuild.
kubectl create configmap localmind-crawler-config \
  --namespace localmind \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml --from-file=config/tiers.yaml

# Crawler-repo-only tokens — localmind-crawler-secret (NOT localmind-secrets,
# which the ai-web-search repo owns in this namespace with a different key
# set).
#   admin-token     Bearer token for the crawler's /hits + /admin/* API
#                   (empty = auth disabled — dev only)
#   cse-api-key / cse-cx / bing-api-key
#                   web-search leg (search/web.py); empty = /web returns 400
# Note: page summaries (crawler/summarize.py) use a LOCAL Ollama model
# (OLLAMA_URL / SUMMARY_MODEL envs in k8s/crawler.yaml) — open-weight only,
# no key needed. The former anthropic-key entry is obsolete (harmless to
# leave in an already-created Secret).
kubectl create secret generic localmind-crawler-secret \
  --namespace localmind \
  --from-literal=admin-token=CHANGEME \
  --from-literal=cse-api-key= \
  --from-literal=cse-cx= \
  --from-literal=bing-api-key=

# Secrets that live with THIS repo only (tokens). Postgres is NOT created here:
# the `localmind-crawler-pg` Secret (user/password + database-url) is created
# by the ai-web-search repo — k8s-manifests/secrets.dev.yaml (dev) or
# k8s-manifests/secrets.prod.template.yaml (prod) — and its
# k8s-manifests/crawler-db.yaml (DatabaseRole + Database) creates the matching
# role + database `localmind_crawler` inside the shared localmind-pg CNPG
# cluster, OPERATOR-managed. Both must be Reconciled before the crawler
# Deployment starts:
#   kubectl -n localmind get databaseroles databases   # both STATUS: Reconciled
