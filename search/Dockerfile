FROM python:3.12-slim
WORKDIR /app
COPY search/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# search imports crawler.db (SessionLocal) and crawler.tierbot (restore_page),
# so the crawler package ships in this image too.
COPY shared ./shared
COPY crawler ./crawler
COPY search ./search
CMD ["uvicorn", "search.app:app", "--host", "0.0.0.0", "--port", "8000"]
