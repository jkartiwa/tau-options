"""Scan log — opt-in (`--log`). Records what a scan saw and what passed, so
screens can be compared against outcomes later. Off by default: the tool is
for finding trades, and history is only worth writing for someone who intends
to read it. SQLite at {TAU_DATA_DIR|~/.local/share/tau}/tau.sqlite3.

`tau rank --log` also records the trade: which strategy definition and which
variant of it produced each pick, with its legs and its figures. Without that
the accumulating corpus could never answer "how did 16-delta strangles do
versus 30-delta jade lizards", which is most of the reason a strategy layer
is worth having.

Definitions are stored once each, keyed by a digest of their serialized form,
and picks point at them. That way editing a strategy does not rewrite the
history of trades chosen under the old version — the two coexist under the
same name, distinguishable by digest.

Still not logged: the catalyst verdict and the price context, so "how did the
names tagged resolved actually do" remains unanswerable off this."""

import hashlib
import json
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
-- strategy_def_id and everything below it are nullable so a symbol that
-- priced nothing still gets a row carrying its reason. A name that passed
-- the screen and had no tradable structure today is part of the record, and
-- an absence would be indistinguishable from never having looked.
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


# Columns added after the table shipped. `_SCHEMA` is CREATE TABLE IF NOT
# EXISTS, so it never reaches a database that already has the table — an
# additive ALTER is the only way an existing log gains a column. Every
# migration here must be non-destructive: no row is rewritten, dropped, or
# backfilled with a guess, and the NULL an old row keeps means "not recorded",
# which is the truth about a scan taken before the column existed.
_MIGRATIONS = (
    ("pick", "bpr_source", "TEXT"),
)

# Named rather than positional, so inserting a column into `_SCHEMA` can never
# silently shift every value one place to the left.
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
    root = Path(
        os.environ.get("TAU_DATA_DIR", Path.home() / ".local/share/tau")
    )
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

    Frozen dataclasses all the way down, so `asdict()` is the identity for
    free; the enums are `StrEnum`s so it serializes to readable values rather
    than integers. The digest is over the sorted JSON, which makes it stable
    across dict ordering and sensitive to any change in a leg or a constraint
    — the two properties a version key needs.
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


def log_picks(scan_id: int, proposals) -> int:
    """One row per priced symbol: the structure that won, with the definition
    and the variant that produced it. Symbols that priced nothing get a row
    too, carrying their reason and nothing else."""
    conn = connect()
    try:
        written = 0
        with conn:
            for p in proposals:
                best = p.best
                cycle = p.cycle
                row = {
                    "scan_id": scan_id,
                    "strategy_def_id": (
                        _strategy_def_id(conn, best.strategy) if best else None
                    ),
                    "symbol": p.symbol,
                    "variant": best.variant if best else None,
                    "expiration": cycle.expiration.isoformat() if cycle else None,
                    "dte": cycle.dte if cycle else None,
                    "underlying": cycle.underlying if cycle else None,
                    "legs_json": json.dumps(_leg_rows(best)) if best else None,
                    "credit": best.credit if best else None,
                    "max_profit": _finite(best.max_profit) if best else None,
                    "bpr": best.bpr if best else None,
                    # Which margin model produced `bpr`, and therefore `roc`
                    # and `annualized_roc` with it. The broker's portfolio
                    # margin and the naked-margin formula are different
                    # numbers for the same trade, so a corpus that compares
                    # capital efficiency across scans has to know which.
                    "bpr_source": best.bpr_source if best else None,
                    "roc": best.roc if best else None,
                    "annualized_roc": best.annualized_roc if best else None,
                    "pop": best.pop if best else None,
                    "spread_cost": best.spread_cost if best else None,
                    "be_over_em": best.be_over_em if best else None,
                    "breakevens": (
                        json.dumps([round(b, 4) for b in best.breakevens])
                        if best
                        else None
                    ),
                    "error": p.error,
                }
                conn.execute(_INSERT_PICK, row)
                written += 1
        return written
    finally:
        conn.close()


def _finite(value: float | None) -> float | None:
    """SQLite stores infinity happily and then reads it back as a number no
    query can reason about. An open profit tail is an absent figure, not a
    huge one."""
    if value is None or value in (float("inf"), float("-inf")):
        return None
    return value
