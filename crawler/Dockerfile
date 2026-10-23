FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY crawler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY crawler ./crawler
COPY config ./config
CMD ["uvicorn", "crawler.app:app", "--host", "0.0.0.0", "--port", "8000"]
