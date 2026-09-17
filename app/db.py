"""Small, auditable SQLite store for profiles and evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]


def database_path() -> Path:
    configured = os.getenv("DATABASE_PATH", "./data/campustrace.sqlite3")
    path = Path(configured)
    if not path.is_absolute():
        path = BASE / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(database_path(), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def connection():
    con = connect()
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize() -> None:
    with connection() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS institutions (
              ror_id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              city TEXT,
              country TEXT,
              official_domain TEXT,
              record_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
              ror_id TEXT PRIMARY KEY REFERENCES institutions(ror_id),
              payload_json TEXT NOT NULL,
              generated_at INTEGER NOT NULL,
              pipeline_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT NOT NULL,
              ror_id TEXT NOT NULL REFERENCES institutions(ror_id),
              title TEXT NOT NULL,
              category TEXT NOT NULL,
              source_url TEXT NOT NULL,
              image_url TEXT,
              license TEXT,
              author TEXT,
              published_at TEXT,
              captured_at TEXT,
              sha1 TEXT,
              dhash TEXT,
              status TEXT NOT NULL,
              reasons_json TEXT NOT NULL,
              retrieved_at INTEGER NOT NULL,
              PRIMARY KEY (ror_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_assets_ror ON assets(ror_id);
            CREATE TABLE IF NOT EXISTS source_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ror_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              outcome TEXT NOT NULL,
              detail TEXT,
              elapsed_ms INTEGER,
              observed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_cache (
              cache_key TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              expires_at INTEGER NOT NULL
            );
            """
        )
        # Old MVP databases used a global asset ID, which lost shared media when
        # two profiles referenced the same Commons file.
        columns = con.execute("PRAGMA table_info(assets)").fetchall()
        if [(row["name"], row["pk"]) for row in columns if row["pk"]] == [("id", 1)]:
            con.executescript("""
                ALTER TABLE assets RENAME TO assets_old;
                CREATE TABLE assets (
                  id TEXT NOT NULL, ror_id TEXT NOT NULL REFERENCES institutions(ror_id),
                  title TEXT NOT NULL, category TEXT NOT NULL, source_url TEXT NOT NULL,
                  image_url TEXT, license TEXT, author TEXT, published_at TEXT,
                  captured_at TEXT, sha1 TEXT, dhash TEXT, status TEXT NOT NULL,
                  reasons_json TEXT NOT NULL, retrieved_at INTEGER NOT NULL,
                  PRIMARY KEY (ror_id, id)
                );
                INSERT INTO assets SELECT * FROM assets_old;
                DROP TABLE assets_old;
                CREATE INDEX IF NOT EXISTS idx_assets_ror ON assets(ror_id);
            """)


def get_cached(key: str) -> Any:
    with connection() as con:
        row = con.execute("SELECT payload_json FROM research_cache WHERE cache_key=? AND expires_at>?", (key, int(time.time()))).fetchone()
    return json.loads(row[0]) if row else None


def set_cached(key: str, value: Any, ttl: int = 3600) -> None:
    with connection() as con:
        con.execute("INSERT OR REPLACE INTO research_cache VALUES (?,?,?)", (key, json.dumps(value, ensure_ascii=False), int(time.time()) + ttl))
        con.execute("DELETE FROM research_cache WHERE expires_at<?", (int(time.time()),))


def get_profile(ror_id: str) -> dict[str, Any] | None:
    with connection() as con:
        row = con.execute(
            "SELECT payload_json, generated_at FROM profiles WHERE ror_id = ?", (ror_id,)
        ).fetchone()
    if not row:
        return None
    profile = json.loads(row["payload_json"])
    profile["cache_age_seconds"] = max(0, int(time.time()) - row["generated_at"])
    return profile


def get_asset(asset_id: str, ror_id: str | None = None) -> dict[str, Any] | None:
    with connection() as con:
        row = con.execute("SELECT * FROM assets WHERE id = ? AND (? IS NULL OR ror_id = ?)", (asset_id, ror_id, ror_id)).fetchone()
        profile_row = con.execute("SELECT payload_json FROM profiles WHERE ror_id = ?", (row["ror_id"],)).fetchone() if row else None
    if not row:
        return None
    if profile_row:
        complete = next((a for a in json.loads(profile_row["payload_json"]).get("assets", []) if a["id"] == asset_id), None)
        if complete:
            return {**complete, "ror_id": row["ror_id"], "retrieved_at": row["retrieved_at"]}
    asset = dict(row)
    asset["reasons"] = json.loads(asset.pop("reasons_json"))
    return asset


def save_profile(profile: dict[str, Any], institution_record: dict[str, Any]) -> None:
    now = int(time.time())
    inst = profile["institution"]
    with connection() as con:
        con.execute(
            """INSERT INTO institutions
                 (ror_id, display_name, city, country, official_domain, record_json, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(ror_id) DO UPDATE SET display_name=excluded.display_name,
                 city=excluded.city, country=excluded.country,
                 official_domain=excluded.official_domain,
                 record_json=excluded.record_json, updated_at=excluded.updated_at""",
            (
                inst["ror_id"], inst["name"], inst.get("city"), inst.get("country"),
                inst.get("official_domain"), json.dumps(institution_record), now,
            ),
        )
        con.execute(
            """INSERT INTO profiles (ror_id, payload_json, generated_at, pipeline_version)
                 VALUES (?, ?, ?, ?) ON CONFLICT(ror_id) DO UPDATE SET
                 payload_json=excluded.payload_json, generated_at=excluded.generated_at,
                 pipeline_version=excluded.pipeline_version""",
            (inst["ror_id"], json.dumps(profile, ensure_ascii=False), now, profile["pipeline_version"]),
        )
        con.execute("DELETE FROM assets WHERE ror_id = ?", (inst["ror_id"],))
        for a in profile["assets"]:
            con.execute(
                """INSERT INTO assets
                   (id,ror_id,title,category,source_url,image_url,license,author,
                    published_at,captured_at,sha1,dhash,status,reasons_json,retrieved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    a["id"], inst["ror_id"], a["title"], a["category"], a["source_url"],
                    a.get("image_url"), a.get("license"), a.get("author"),
                    a.get("published_at"), a.get("captured_at"), a.get("sha1"),
                    a.get("dhash"), a["status"], json.dumps(a["reasons"], ensure_ascii=False), now,
                ),
            )
        for event in profile.get("source_events", []):
            con.execute(
                "INSERT INTO source_events (ror_id,provider,outcome,detail,elapsed_ms,observed_at) VALUES (?,?,?,?,?,?)",
                (inst["ror_id"], event["provider"], event["outcome"], event.get("detail"), event.get("elapsed_ms"), now),
            )
