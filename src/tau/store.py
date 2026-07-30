"""Scan log — opt-in (`tau scan --log`). Records what a scan saw and what
passed, so screens can be compared against outcomes later. Off by default:
the tool is for finding trades, and history is only worth writing for
someone who intends to read it. SQLite at
{TAU_DATA_DIR|~/.local/share/tau}/tau.sqlite3.

Note that this records the screen, not the trade — no strikes, credit, or
catalyst verdict. Answering "how did the names tagged resolved actually do"
would need those logged too."""

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
