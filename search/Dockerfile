FROM python:3.12-slim
WORKDIR /app
COPY search/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# search imports crawler.db (SessionLocal), crawler.tierbot (restore_page) and
# crawler.fetch, so the crawler package ships in this image too — and its
# import-chain deps (beautifulsoup4, pyyaml) live in this requirements.txt.
# crawler/requirements.txt additionally has playwright, which this
# app never imports: don't "simplify" to installing that file.
COPY shared ./shared
COPY crawler ./crawler
COPY search ./search
CMD ["uvicorn", "search.app:app", "--host", "0.0.0.0", "--port", "8000"]
