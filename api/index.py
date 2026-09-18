"""Vercel entrypoint: the same FastAPI app, initialised eagerly.

Vercel's Python runtime may not run ASGI lifespan events, and its filesystem
is read-only except /tmp, so the SQLite cache lives there (per instance,
rebuilt after a cold start) and the schema is created at import time.
"""

import os

os.environ.setdefault("DATABASE_PATH", "/tmp/campustrace.sqlite3")

from app import db  # noqa: E402
from app.main import app  # noqa: E402,F401  (Vercel serves this ASGI object)

db.initialize()
