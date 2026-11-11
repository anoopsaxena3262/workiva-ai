import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
import os
DB_PATH = Path(os.environ.get("ER_DB", BASE / "er_board.db"))


def now():
    # UTC with Z, same convention as the source snapshot; the board converts to local time for display.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA_VERSION = 2


def init_db():
    conn = connect()
    has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='patients'").fetchone()[0]
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if has_tables and ver != SCHEMA_VERSION:
        conn.close()
        raise SystemExit(f"{DB_PATH} was created by an older version of this app (schema v{ver}, need v{SCHEMA_VERSION}).\n"
                         "No automatic migration. Move the file aside and re-import your JSON; the old file stays untouched.")
    conn.executescript((BASE / "schema.sql").read_text())
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()


def dump_all(conn):
    out = {}
    for t in ("facilities", "doctors", "rooms", "patients", "vitals", "photos"):
        out[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    return out


def audit(conn, actor, action, entity, entity_id=None, detail=None):
    """Append-only audit entry. Runs inside the caller's transaction so a
    change and its audit record commit (or roll back) together."""
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
        (now(), actor, action, entity, None if entity_id is None else str(entity_id),
         json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None),
    )
