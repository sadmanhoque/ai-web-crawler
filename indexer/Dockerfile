FROM python:3.12-slim
WORKDIR /app
COPY indexer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY indexer ./indexer
COPY config/profiles.yaml ./config/profiles.yaml
CMD ["python", "-m", "indexer.app"]
