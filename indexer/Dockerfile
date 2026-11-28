FROM python:3.12-slim
WORKDIR /app
COPY indexer/requirements.txt .
# CPU-only torch (minikube, no GPU passthrough) — same policy as the
# backend image: the default PyPI torch drags in ~3GB of nvidia-* CUDA
# wheels that can never be used here and bloat the build (see
# Dockerfile.backend in the ai-web-search repo for the original rationale).
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY indexer ./indexer
COPY config/profiles.yaml ./config/profiles.yaml
CMD ["python", "-m", "indexer.app"]
