"""SQLite 저장소.

설계 원칙 (K-Trader에서 얻은 교훈):
- **누적 카운터를 두지 않는다.** 파생값은 원본에서 매번 재계산한다. 드리프트가 조용히 쌓이는 것을 막는다.
- **소스를 컬럼으로 기록한다.** 수정주가 소스와 원본가 소스를 섞으면 액면분할 종목에서 수익률이 튄다.
- **스키마 버전을 관리한다.** K-Trader는 `PRAGMA user_version`이 없어 마이그레이션이 수작업 ALTER 루프였다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd

from data import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    code             TEXT    NOT NULL,
    date             TEXT    NOT NULL,
    open             INTEGER NOT NULL,
    high             INTEGER NOT NULL,
    low              INTEGER NOT NULL,
    close            INTEGER NOT NULL,
    volume           INTEGER NOT NULL,
    foreign_hold_pct REAL,
    halted           INTEGER NOT NULL DEFAULT 0,
    source           TEXT    NOT NULL,
    adjusted         INTEGER NOT NULL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv(date);

CREATE TABLE IF NOT EXISTS listing (
    code         TEXT PRIMARY KEY,
    name         TEXT,
    market       TEXT,
    sector       TEXT,
    industry     TEXT,
    listing_date TEXT,
    market_cap   REAL,
    shares       REAL,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    is_spac      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delisting (
    code           TEXT NOT NULL,
    delisting_date TEXT NOT NULL,
    name           TEXT,
    market         TEXT,
    listing_date   TEXT,
    reason         TEXT,
    to_code        TEXT,
    to_name        TEXT,
    PRIMARY KEY (code, delisting_date)
);
CREATE INDEX IF NOT EXISTS idx_delisting_date ON delisting(delisting_date);

CREATE TABLE IF NOT EXISTS flows (
    code             TEXT    NOT NULL,
    date             TEXT    NOT NULL,
    inst_net_qty     INTEGER,
    foreign_net_qty  INTEGER,
    foreign_hold_qty INTEGER,
    foreign_hold_pct REAL,
    source           TEXT    NOT NULL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_flows_date ON flows(date);

CREATE TABLE IF NOT EXISTS indicators (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    payload TEXT NOT NULL,   -- JSON. 지표 세트가 자주 바뀌므로 컬럼으로 굳히지 않는다
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_indicators_date ON indicators(date);

-- 배치 실행 기록. 어떤 날 무엇이 실패했는지 남지 않으면 결손을 발견할 수 없다.
CREATE TABLE IF NOT EXISTS ingest_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    task       TEXT NOT NULL,
    target     TEXT,
    status     TEXT NOT NULL,   -- ok | fail | skip
    rows       INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_started ON ingest_log(started_at);
"""


@contextmanager
def connect(db_path: Path | None = None):
    config.ensure_dirs()
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif current != SCHEMA_VERSION:
        raise RuntimeError(
            f"스키마 버전 불일치: DB={current}, 코드={SCHEMA_VERSION}. "
            "마이그레이션을 작성하기 전에는 진행하지 않는다."
        )


# ── 쓰기 ────────────────────────────────────────────────


def upsert_ohlcv(
    conn: sqlite3.Connection,
    code: str,
    df: pd.DataFrame,
    *,
    source: str = config.CANONICAL_OHLCV_SOURCE,
    adjusted: bool = True,
) -> int:
    if df.empty:
        return 0
    rows = [
        (
            code,
            r.date.isoformat(),
            int(r.open),
            int(r.high),
            int(r.low),
            int(r.close),
            int(r.volume),
            None if pd.isna(r.foreign_hold_pct) else float(r.foreign_hold_pct),
            int(bool(r.halted)),
            source,
            int(adjusted),
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO ohlcv
           (code, date, open, high, low, close, volume, foreign_hold_pct, halted, source, adjusted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(code, date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume,
             foreign_hold_pct=excluded.foreign_hold_pct, halted=excluded.halted,
             source=excluded.source, adjusted=excluded.adjusted""",
        rows,
    )
    return len(rows)


def replace_listing(conn: sqlite3.Connection, df: pd.DataFrame, *, updated_at: str) -> int:
    """종목 마스터는 스냅샷이므로 통째로 교체한다."""
    if df.empty:
        raise ValueError("빈 종목 마스터로 교체하지 않는다")
    conn.execute("DELETE FROM listing")
    rows = [
        (
            r.code,
            r.name,
            r.market,
            r.sector,
            r.industry,
            r.listing_date.isoformat() if pd.notna(r.listing_date) else None,
            None if pd.isna(r.market_cap) else float(r.market_cap),
            None if pd.isna(r.shares) else float(r.shares),
            int(bool(r.is_preferred)),
            int(bool(r.is_spac)),
            updated_at,
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO listing
           (code,name,market,sector,industry,listing_date,market_cap,shares,
            is_preferred,is_spac,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def upsert_delisting(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (
            r.code,
            r.delisting_date.isoformat() if pd.notna(r.delisting_date) else "",
            r.name,
            r.market,
            r.listing_date.isoformat() if pd.notna(r.listing_date) else None,
            r.reason,
            r.to_code,
            r.to_name,
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO delisting
           (code,delisting_date,name,market,listing_date,reason,to_code,to_name)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(code,delisting_date) DO UPDATE SET
             name=excluded.name, reason=excluded.reason,
             to_code=excluded.to_code, to_name=excluded.to_name""",
        rows,
    )
    return len(rows)


def upsert_flows(conn: sqlite3.Connection, code: str, df: pd.DataFrame, *, source: str) -> int:
    if df.empty:
        return 0
    rows = [
        (
            code,
            r.date.isoformat(),
            int(r.inst_net_qty),
            int(r.foreign_net_qty),
            int(r.foreign_hold_qty),
            None if pd.isna(r.foreign_hold_pct) else float(r.foreign_hold_pct),
            source,
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO flows
           (code,date,inst_net_qty,foreign_net_qty,foreign_hold_qty,foreign_hold_pct,source)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(code,date) DO UPDATE SET
             inst_net_qty=excluded.inst_net_qty,
             foreign_net_qty=excluded.foreign_net_qty,
             foreign_hold_qty=excluded.foreign_hold_qty,
             foreign_hold_pct=excluded.foreign_hold_pct""",
        rows,
    )
    return len(rows)


def upsert_indicators(conn: sqlite3.Connection, code: str, on: date, payload_json: str) -> None:
    conn.execute(
        """INSERT INTO indicators (code,date,payload) VALUES (?,?,?)
           ON CONFLICT(code,date) DO UPDATE SET payload=excluded.payload""",
        (code, on.isoformat(), payload_json),
    )


def log_ingest(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    task: str,
    target: str | None,
    status: str,
    rows: int | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ingest_log (started_at,task,target,status,rows,detail) VALUES (?,?,?,?,?,?)",
        (started_at, task, target, status, rows, detail),
    )


# ── 읽기 ────────────────────────────────────────────────


def load_ohlcv(
    conn: sqlite3.Connection,
    code: str,
    *,
    exclude_halted: bool = True,
) -> pd.DataFrame:
    """지표 계산용 일봉 로드.

    exclude_halted=True 가 기본이다. 거래정지일(0값 행)을 그대로 넣으면
    ATR·RSI·볼린저가 오염된다. 거래정지 여부 자체가 필요할 때만 False로 둔다.
    """
    sql = "SELECT date, open, high, low, close, volume, foreign_hold_pct, halted FROM ohlcv WHERE code=?"
    if exclude_halted:
        sql += " AND halted=0 AND open>0 AND volume>0"
    sql += " ORDER BY date"
    df = pd.read_sql_query(sql, conn, params=(code,))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def last_ohlcv_date(conn: sqlite3.Connection, code: str) -> date | None:
    row = conn.execute("SELECT MAX(date) FROM ohlcv WHERE code=?", (code,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def tradable_codes(
    conn: sqlite3.Connection,
    *,
    exclude_preferred: bool = True,
    exclude_spac: bool = True,
    min_market_cap: float | None = None,
) -> list[str]:
    sql = "SELECT code FROM listing WHERE 1=1"
    params: list = []
    if exclude_preferred:
        sql += " AND is_preferred=0"
    if exclude_spac:
        sql += " AND is_spac=0"
    if min_market_cap is not None:
        sql += " AND market_cap >= ?"
        params.append(min_market_cap)
    sql += " ORDER BY market_cap DESC"
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def delisted_codes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT code FROM delisting").fetchall()}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in ("ohlcv", "listing", "delisting", "flows", "indicators"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def chunked(items: Iterable, size: int):
    buf = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
