# Crawler config — same namespace as the ai-web-search deployment (localmind).

kubectl create configmap localmind-config \
  --namespace localmind \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml

# Secrets that live with THIS repo only (tokens). Postgres is NOT created here:
# the `localmind-crawler-pg` Secret (user/password + database-url) is created
# by the ai-web-search repo — k8s-manifests/secrets.dev.yaml (dev) or
# k8s-manifests/secrets.prod.template.yaml (prod) — and its
# k8s-manifests/pg-crawler.yaml Job creates the matching role + database
# `localmind_crawler` inside the shared localmind-pg CNPG cluster.
# That Job must have COMPLETED before the crawler Deployment starts:
#   kubectl -n localmind get job pg-crawler-db-init
kubectl create secret generic localmind-secrets \
  --namespace localmind \
  --from-literal=admin-token=CHANGEME --from-literal=cloudflare-token=... --from-literal=anthropic-key=...
