"""Persistent Comic Vine response cache and cross-process rolling request budget."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
WINDOW_SECONDS = 60 * 60
DEFAULT_REQUESTS_PER_HOUR = 160
DEFAULT_DB_PATH = r"D:\ComicStaging\_state\comicvine_cache.sqlite"


class ComicVineBudgetExceeded(RuntimeError):
    def __init__(self, retry_at):
        self.retry_at = float(retry_at)
        super().__init__(f"Comic Vine request budget exhausted until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.retry_at))}")


_db_path = os.environ.get("COMICVINE_CACHE_DB", DEFAULT_DB_PATH)
_request_limit = int(os.environ.get("COMICVINE_REQUESTS_PER_HOUR", DEFAULT_REQUESTS_PER_HOUR))


def configure(db_path=None, requests_per_hour=None):
    global _db_path, _request_limit
    if db_path:
        _db_path = str(db_path)
    if requests_per_hour is not None:
        _request_limit = max(1, int(requests_per_hour))


def _connect():
    path = Path(_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS responses (cache_key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS requests (key_hash TEXT NOT NULL, requested_at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS requests_by_key_time ON requests(key_hash, requested_at)")
    return conn


def _key_hash(api_key):
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _cache_key(api_key, endpoint, params):
    canonical = json.dumps([endpoint, sorted((str(k), str(v)) for k, v in params.items())],
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((_key_hash(api_key) + "\0" + canonical).encode("utf-8")).hexdigest()


def _cached(cache_key, now):
    conn = _connect()
    try:
        row = conn.execute("SELECT fetched_at, body FROM responses WHERE cache_key=?", (cache_key,)).fetchone()
        if not row:
            return None
        if now - row[0] > CACHE_TTL_SECONDS:
            conn.execute("DELETE FROM responses WHERE cache_key=?", (cache_key,))
            conn.commit()
            return None
        return json.loads(row[1])
    finally:
        conn.close()


def _reserve_request(key_hash, now):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM requests WHERE key_hash=? AND requested_at<=?", (key_hash, now - WINDOW_SECONDS))
        row = conn.execute("SELECT COUNT(*), MIN(requested_at) FROM requests WHERE key_hash=?", (key_hash,)).fetchone()
        if row[0] >= _request_limit:
            retry_at = float(row[1]) + WINDOW_SECONDS + 1.0
            conn.rollback()
            raise ComicVineBudgetExceeded(retry_at)
        conn.execute("INSERT INTO requests(key_hash, requested_at) VALUES(?,?)", (key_hash, now))
        conn.commit()
    finally:
        conn.close()


def get_json(api_key, endpoint, params, fetch):
    """Return a cached Comic Vine response, or reserve one request and fetch it."""
    now = time.time()
    cache_key = _cache_key(api_key, endpoint, params)
    result = _cached(cache_key, now)
    if result is not None:
        return result
    _reserve_request(_key_hash(api_key), now)
    result = fetch()
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO responses(cache_key, fetched_at, body) VALUES(?,?,?)",
                     (cache_key, time.time(), body))
        conn.commit()
    finally:
        conn.close()
    return result
