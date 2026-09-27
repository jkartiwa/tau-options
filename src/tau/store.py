"""Scan log, opt-in (`--log`). SQLite at
{TAU_DATA_DIR|~/.local/share/tau}/tau.sqlite3.

`tau scan --log` records what the screen saw and what passed. `tau rank --log`
also records each symbol's pick: the strategy definition and variant that
produced it, with its legs and figures, so results can later be compared by
strategy.

Definitions are stored once each, keyed by a digest of their serialized form.
Editing a strategy therefore does not rewrite the history of picks made under
the old version: both versions coexist under one name, told apart by digest.
"""

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import asdict
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
CREATE TABLE IF NOT EXISTS strategy_def (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    spec_json TEXT NOT NULL,
    first_seen TEXT NOT NULL
);
-- Everything from strategy_def_id down is nullable: a symbol that priced
-- nothing still gets a row carrying its error, since a missing row would be
-- indistinguishable from never having looked.
CREATE TABLE IF NOT EXISTS pick (
    scan_id INTEGER NOT NULL REFERENCES scan(id),
    strategy_def_id INTEGER REFERENCES strategy_def(id),
    symbol TEXT NOT NULL,
    variant TEXT,
    expiration TEXT,
    dte INTEGER,
    underlying REAL,
    legs_json TEXT,
    credit REAL,
    max_profit REAL,
    bpr REAL,
    bpr_source TEXT,
    roc REAL,
    annualized_roc REAL,
    pop REAL,
    spread_cost REAL,
    be_over_em REAL,
    breakevens TEXT,
    error TEXT
);
"""


# Columns added after the table shipped. `_SCHEMA` never alters an existing
# table, so these are applied with ALTER TABLE ADD COLUMN. Old rows keep NULL,
# meaning "not recorded"; no migration may rewrite or backfill rows.
_MIGRATIONS = (("pick", "bpr_source", "TEXT"),)

# Named rather than positional, so adding a column to `_SCHEMA` cannot shift
# values into the wrong columns.
_PICK_COLUMNS = (
    "scan_id",
    "strategy_def_id",
    "symbol",
    "variant",
    "expiration",
    "dte",
    "underlying",
    "legs_json",
    "credit",
    "max_profit",
    "bpr",
    "bpr_source",
    "roc",
    "annualized_roc",
    "pop",
    "spread_cost",
    "be_over_em",
    "breakevens",
    "error",
)
_INSERT_PICK = (
    f"INSERT INTO pick ({', '.join(_PICK_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _PICK_COLUMNS)})"
)


def db_path() -> Path:
    root = Path(os.environ.get("TAU_DATA_DIR", Path.home() / ".local/share/tau"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "tau.sqlite3"


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing log up to the current column set, additively."""
    for table, column, decl in _MIGRATIONS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    with conn:
        _migrate(conn)
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


def strategy_identity(strategy) -> tuple[str, str]:
    """A strategy's serialized form and a digest of it.

    The digest is over key-sorted JSON of `asdict()`, so it is stable across
    dict ordering and changes with any edit to a leg or a constraint.
    """
    spec = json.dumps(asdict(strategy), sort_keys=True, default=str)
    return spec, hashlib.sha256(spec.encode()).hexdigest()[:16]


def _strategy_def_id(conn: sqlite3.Connection, strategy) -> int:
    spec, digest = strategy_identity(strategy)
    row = conn.execute(
        "SELECT id FROM strategy_def WHERE digest = ?", (digest,)
    ).fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO strategy_def (name, digest, spec_json, first_seen) "
        "VALUES (?, ?, ?, ?)",
        (strategy.name, digest, spec, datetime.now(UTC).isoformat()),
    )
    return cur.lastrowid


def _leg_rows(structure) -> list[dict]:
    return [
        {
            "id": b.spec.id,
            "type": str(b.spec.type),
            "side": str(b.spec.side),
            "qty": b.spec.qty,
            "strike": b.leg.strike,
            "occ": b.leg.occ,
            "delta": b.leg.delta,
            "bid": b.leg.bid,
            "ask": b.leg.ask,
            "off_target": b.off_target,
            "strike_miss": b.strike_miss,
        }
        for b in structure.legs
    ]


def _pick_row(scan_id: int, strategy_def_id: int | None, proposal) -> dict:
    """The insert row for one proposal: its winning structure, or only its
    error when it priced nothing."""
    row = dict.fromkeys(_PICK_COLUMNS)
    row.update(
        scan_id=scan_id,
        strategy_def_id=strategy_def_id,
        symbol=proposal.symbol,
        error=proposal.error,
    )
    if cycle := proposal.cycle:
        row.update(
            expiration=cycle.expiration.isoformat(),
            dte=cycle.dte,
            underlying=cycle.underlying,
        )
    if best := proposal.best:
        row.update(
            variant=best.variant,
            legs_json=json.dumps(_leg_rows(best)),
            credit=best.credit,
            max_profit=_finite(best.max_profit),
            bpr=best.bpr,
            # Which margin model produced `bpr` (and so `roc`): broker and
            # formula figures differ for the same trade.
            bpr_source=best.bpr_source,
            roc=best.roc,
            annualized_roc=best.annualized_roc,
            pop=best.pop,
            spread_cost=best.spread_cost,
            be_over_em=best.be_over_em,
            breakevens=json.dumps([round(b, 4) for b in best.breakevens]),
        )
    return row


def log_picks(scan_id: int, proposals) -> int:
    """Write one pick row per proposal and return how many were written."""
    conn = connect()
    try:
        rows = []
        with conn:
            for p in proposals:
                def_id = _strategy_def_id(conn, p.best.strategy) if p.best else None
                rows.append(_pick_row(scan_id, def_id, p))
            conn.executemany(_INSERT_PICK, rows)
        return len(rows)
    finally:
        conn.close()


def _finite(value: float | None) -> float | None:
    """Infinity as NULL: SQLite stores it, but queries cannot reason about it.
    An unbounded profit is an absent figure, not a huge one."""
    return None if value is None or math.isinf(value) else value
