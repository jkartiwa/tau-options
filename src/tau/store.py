"""Scan log — every scan writes what it saw and what passed, so the scanner's
picks accumulate as a queryable corpus from day one (the scoreboard reads
this later). SQLite at {TAU_DATA_DIR|~/.local/share/tau}/tau.sqlite3."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tau.screen import Candidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    params_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_result (
    scan_id INTEGER NOT NULL REFERENCES scan(id),
    symbol TEXT NOT NULL,
    ivr REAL,
    ivp REAL,
    iv30 REAL,
    hv30 REAL,
    liquidity INTEGER,
    beta REAL,
    earnings_date TEXT,
    passed INTEGER NOT NULL,
    reasons TEXT NOT NULL
);
"""


def db_path() -> Path:
    root = Path(
        os.environ.get("TAU_DATA_DIR", Path.home() / ".local/share/tau")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "tau.sqlite3"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def log_scan(params: dict, candidates: list[Candidate]) -> int:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO scan (ts, params_json) VALUES (?, ?)",
                (datetime.now(UTC).isoformat(), json.dumps(params)),
            )
            scan_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO scan_result VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        scan_id,
                        c.symbol,
                        c.ivr,
                        c.ivp,
                        c.iv30,
                        c.hv30,
                        c.liquidity,
                        c.beta,
                        c.earnings_date.isoformat() if c.earnings_date else None,
                        int(c.passed),
                        "; ".join(c.excluded),
                    )
                    for c in candidates
                ],
            )
        return scan_id
    finally:
        conn.close()
