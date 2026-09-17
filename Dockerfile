# CampusTrace — single-container deployment.
# Works unchanged on Render, Railway, Fly.io and any plain Docker host.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/campustrace.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY data/atlas_nu.json data/atlas_upenn.json ./data/

# SQLite lives on a mounted volume where the host provides one; without a
# volume the cache is simply rebuilt after a restart, which is not fatal.
RUN mkdir -p /data

EXPOSE 8000
# One worker on purpose: the in-flight coalescing and the Wikimedia rate
# limiter are per-process, and a hackathon profile build is I/O bound anyway.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
