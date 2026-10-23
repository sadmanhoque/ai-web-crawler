kubectl create configmap localmind-config \
  --from-file=config/profiles.yaml --from-file=config/sites.yaml
kubectl create secret generic localmind-secrets \
  --from-literal=database-url='postgresql+psycopg://user:pass@postgres/localmind' \
  --from-literal=admin-token=CHANGEME --from-literal=cloudflare-token=... --from-literal=anthropic-key=...
