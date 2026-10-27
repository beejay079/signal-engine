"""
store.py - keeps settings, alerts, locked setups and your trade journal.

  DATABASE_URL set (free Postgres, e.g. Neon)  -> saved permanently, survives redeploys
  not set                                       -> saved to a file; lost when Render redeploys
"""
import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_conn = None
last_error = None
DIR = Path(os.getenv("DATA_DIR", Path(__file__).with_name("data"))) / "kv"


def _db():
    global _conn
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    if _conn is None or _conn.closed:
        import psycopg
        _conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
        _conn.execute("create table if not exists kv (k text primary key, v jsonb not null, "
                      "updated timestamptz not null default now())")
    return _conn


def _drop():
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


def backend():
    return "database" if os.getenv("DATABASE_URL") else "file"


def get(k, default=None):
    global last_error
    with _lock:
        if os.getenv("DATABASE_URL"):
            for _ in range(2):
                try:
                    row = _db().execute("select v from kv where k = %s", (k,)).fetchone()
                    last_error = None
                    return row[0] if row else default
                except Exception as e:
                    last_error = f"database: {e}"[:200]
                    _drop()
            return default
        try:
            return json.loads((DIR / f"{k}.json").read_text())
        except Exception:
            return default


def put(k, v):
    global last_error
    with _lock:
        if os.getenv("DATABASE_URL"):
            from psycopg.types.json import Jsonb
            for _ in range(2):
                try:
                    _db().execute("insert into kv (k, v) values (%s, %s) on conflict (k) do update "
                                  "set v = excluded.v, updated = now()", (k, Jsonb(v, dumps=lambda o: json.dumps(o, default=float))))
                    last_error = None
                    return True
                except Exception as e:
                    last_error = f"database: {e}"[:200]
                    _drop()
            return False
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            tmp = DIR / f"{k}.json.tmp"
            tmp.write_text(json.dumps(v, default=float))
            tmp.replace(DIR / f"{k}.json")
            return True
        except Exception as e:
            last_error = f"file: {e}"[:200]
            return False
