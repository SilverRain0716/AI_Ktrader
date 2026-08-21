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

SCHEMA_VERSION = 4

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

CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no  TEXT PRIMARY KEY,       -- DART 접수번호. 멱등키.
    rcept_dt  TEXT NOT NULL,          -- YYYYMMDD
    corp_code TEXT NOT NULL,          -- DART 고유번호 (종목코드와 다르다)
    code      TEXT,                   -- 6자리 종목코드. 비상장 법인은 NULL
    corp_name TEXT,
    corp_cls  TEXT,                   -- Y 유가 / K 코스닥
    report_nm TEXT NOT NULL,
    category  TEXT NOT NULL,          -- schemas/briefing.schema.json 의 enum과 일치
    material  INTEGER NOT NULL,       -- 노이즈 필터 통과 여부
    filer     TEXT,
    remark    TEXT,
    url       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disc_dt ON disclosures(rcept_dt);
CREATE INDEX IF NOT EXISTS idx_disc_code ON disclosures(code, rcept_dt);

-- ── 브리핑 구조화 (Phase 2) ─────────────────────────────
CREATE TABLE IF NOT EXISTS briefings (
    briefing_id    TEXT PRIMARY KEY,   -- YYYY-MM-DD-<stem>
    day            TEXT NOT NULL,
    stem           TEXT NOT NULL,      -- 원본 파일명. 스케줄 개편 이력 추적용
    kind           TEXT NOT NULL,      -- 정규화 종류
    published_at   TEXT NOT NULL,
    market         TEXT NOT NULL,
    source_url     TEXT,
    summary        TEXT,
    heading        TEXT,
    sections       TEXT NOT NULL,      -- JSON. 원문 섹션 통째 보존 — 재추출 시 GitLab 재조회 불필요
    disclosure_refs TEXT NOT NULL,     -- JSON. DART 접수번호 참조 (공시 본문은 disclosures 테이블이 정본)
    parse_warnings TEXT NOT NULL,      -- JSON. 비어있지 않으면 AI 판단 시 신뢰도를 낮춘다
    view_count     INTEGER NOT NULL,
    ingested_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brief_day ON briefings(day);
CREATE INDEX IF NOT EXISTS idx_brief_kind ON briefings(kind, day);

CREATE TABLE IF NOT EXISTS briefing_views (
    briefing_id      TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    day              TEXT NOT NULL,
    kind             TEXT NOT NULL,
    market           TEXT NOT NULL,
    code             TEXT,             -- 6자리 한국 종목코드. 매핑 실패 시 NULL
    symbol           TEXT,             -- 미국 티커
    name             TEXT,
    stance           TEXT NOT NULL,    -- 주목 | 조건부 | 경계 | 회피
    stance_inherited INTEGER NOT NULL,
    confidence       TEXT,             -- 원문에 없으면 NULL
    confidence_note  TEXT,
    catalyst         TEXT,
    reasons          TEXT NOT NULL,    -- JSON 배열
    invalidation     TEXT,
    check_at         TEXT,
    kr_links         TEXT,             -- JSON 배열
    sources          TEXT,             -- JSON 배열
    raw              TEXT,
    PRIMARY KEY (briefing_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_view_code ON briefing_views(code, day);
CREATE INDEX IF NOT EXISTS idx_view_day ON briefing_views(day, kind);

-- ── 페이퍼 포지션 (Phase 3~5) ───────────────────────────
-- 실행 계층이 생기기 전까지 포지션의 정본. 파생값(평가손익·보유일수·비중)은
-- 저장하지 않고 조회 시 매번 재계산한다 — 누적 카운터를 두지 않는 원칙.
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id       TEXT PRIMARY KEY,
    code              TEXT NOT NULL,
    name              TEXT,
    qty               INTEGER NOT NULL,
    avg_price         INTEGER NOT NULL,
    opened_at         TEXT NOT NULL,
    closed_at         TEXT,              -- NULL 이면 보유 중
    entry_decision_id TEXT,              -- 이 포지션을 만든 결정
    entry_thesis      TEXT,              -- 진입 근거. 이게 아직 유효한지가 보유 판단의 핵심
    invalidation      TEXT,              -- 진입 시 설정한 무효화 조건
    invalidation_hit  INTEGER NOT NULL DEFAULT 0,
    stop_price        INTEGER,
    target_price      INTEGER,
    max_hold_days     INTEGER,
    exit_reason       TEXT,
    exit_price        INTEGER,
    realized_pnl_krw  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pos_open ON paper_positions(closed_at, code);

-- ── 컨텍스트 팩 (감사 추적) ─────────────────────────────
-- 결정 JSON이 pack_id 를 참조한다. "왜 그때 그렇게 판단했는가"를 물으면
-- 그 시점의 입력 전체를 그대로 복원할 수 있어야 한다.
CREATE TABLE IF NOT EXISTS context_packs (
    pack_id        TEXT PRIMARY KEY,
    cycle          TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    universe_size  INTEGER NOT NULL,
    position_count INTEGER NOT NULL,
    view_count     INTEGER NOT NULL,
    warning_count  INTEGER NOT NULL,
    est_tokens     INTEGER,
    payload        TEXT NOT NULL      -- 팩 전문 JSON
);
CREATE INDEX IF NOT EXISTS idx_pack_cycle ON context_packs(cycle, generated_at);

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


# 전진 마이그레이션. 키는 "도달할 버전", 값은 그 버전으로 올리는 SQL.
# _SCHEMA 는 전부 IF NOT EXISTS 라 새 테이블 추가는 재실행만으로 반영된다.
# ALTER 가 필요한 변경만 여기에 적는다.
_MIGRATIONS: dict[int, str] = {
    2: "",  # disclosures 테이블 추가 — _SCHEMA 재실행으로 충분
    3: "",  # briefings·briefing_views 추가 — _SCHEMA 재실행으로 충분
    4: "",  # paper_positions·context_packs 추가 — _SCHEMA 재실행으로 충분
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"DB 스키마({current})가 코드({SCHEMA_VERSION})보다 최신이다. "
            "구버전 코드로 최신 DB를 건드리지 않는다."
        )

    for version in range(current + 1, SCHEMA_VERSION + 1):
        if version not in _MIGRATIONS:
            raise RuntimeError(f"v{version} 마이그레이션이 정의되지 않았다.")
        sql = _MIGRATIONS[version]
        if sql.strip():
            conn.executescript(sql)
        log.info("스키마 마이그레이션 v%d → v%d", version - 1, version)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


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


def upsert_disclosures(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """접수번호가 멱등키다. 같은 날 여러 번 돌려도 중복되지 않는다."""
    if df.empty:
        return 0
    rows = [
        (
            r.rcept_no,
            r.rcept_dt,
            r.corp_code,
            r.code,
            r.corp_name,
            r.corp_cls,
            r.report_nm,
            r.category,
            int(bool(r.material)),
            r.filer,
            r.remark,
            r.url,
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO disclosures
           (rcept_no,rcept_dt,corp_code,code,corp_name,corp_cls,
            report_nm,category,material,filer,remark,url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rcept_no) DO UPDATE SET
             report_nm=excluded.report_nm, category=excluded.category,
             material=excluded.material, remark=excluded.remark""",
        rows,
    )
    return len(rows)


def load_disclosures(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    codes: Iterable[str] | None = None,
    material_only: bool = True,
) -> pd.DataFrame:
    """컨텍스트 팩의 disclosures 블록에 넣을 공시를 뽑는다."""
    sql = "SELECT * FROM disclosures WHERE rcept_dt BETWEEN ? AND ?"
    params: list = [start.strftime("%Y%m%d"), end.strftime("%Y%m%d")]
    if material_only:
        sql += " AND material=1"
    code_list = list(codes) if codes is not None else None
    if code_list:
        sql += f" AND code IN ({','.join('?' * len(code_list))})"
        params.extend(code_list)
    sql += " ORDER BY rcept_dt DESC, rcept_no"
    return pd.read_sql_query(sql, conn, params=params)


def upsert_briefing(conn: sqlite3.Connection, parsed: dict, *, stem: str, ingested_at: str) -> int:
    """브리핑 1건과 그 관점들을 저장한다. briefing_id 가 멱등키다."""
    import json as _json

    bid = parsed["briefing_id"]
    conn.execute(
        """INSERT INTO briefings
           (briefing_id,day,stem,kind,published_at,market,source_url,summary,heading,
            sections,disclosure_refs,parse_warnings,view_count,ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(briefing_id) DO UPDATE SET
             kind=excluded.kind, published_at=excluded.published_at,
             summary=excluded.summary, heading=excluded.heading,
             sections=excluded.sections, disclosure_refs=excluded.disclosure_refs,
             parse_warnings=excluded.parse_warnings, view_count=excluded.view_count,
             ingested_at=excluded.ingested_at""",
        (
            bid,
            bid[:10],
            stem,
            parsed["kind"],
            parsed["published_at"],
            parsed["market"],
            parsed.get("source_url"),
            parsed.get("summary"),
            parsed.get("heading"),
            _json.dumps(parsed.get("sections", {}), ensure_ascii=False),
            _json.dumps(parsed.get("disclosures", []), ensure_ascii=False),
            _json.dumps(parsed.get("parse_warnings", []), ensure_ascii=False),
            len(parsed.get("views", [])),
            ingested_at,
        ),
    )
    # 재파싱 시 관점이 줄어들 수 있으므로 통째로 교체한다
    conn.execute("DELETE FROM briefing_views WHERE briefing_id=?", (bid,))
    rows = []
    for i, v in enumerate(parsed.get("views", [])):
        cat = v.get("catalyst")
        rows.append(
            (
                bid,
                i,
                bid[:10],
                parsed["kind"],
                v.get("market", parsed["market"]),
                v.get("code"),
                v.get("symbol"),
                v.get("name"),
                v["stance"],
                int(bool(v.get("stance_inherited"))),
                v.get("confidence"),
                v.get("confidence_note"),
                (cat or {}).get("summary") if isinstance(cat, dict) else cat,
                _json.dumps(v.get("reasons", []), ensure_ascii=False),
                v.get("invalidation"),
                v.get("check_at"),
                _json.dumps(v.get("kr_links", []), ensure_ascii=False),
                _json.dumps(v.get("sources", []), ensure_ascii=False),
                v.get("raw"),
            )
        )
    if rows:
        conn.executemany(
            """INSERT INTO briefing_views
               (briefing_id,seq,day,kind,market,code,symbol,name,stance,stance_inherited,
                confidence,confidence_note,catalyst,reasons,invalidation,check_at,
                kr_links,sources,raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return len(rows)


def briefing_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT briefing_id FROM briefings").fetchall()}


def load_views(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    market: str | None = None,
    stances: Iterable[str] | None = None,
) -> pd.DataFrame:
    """컨텍스트 팩용 관점 조회."""
    sql = "SELECT * FROM briefing_views WHERE day BETWEEN ? AND ?"
    params: list = [start.isoformat(), end.isoformat()]
    if market:
        sql += " AND market=?"
        params.append(market)
    st = list(stances) if stances else None
    if st:
        sql += f" AND stance IN ({','.join('?' * len(st))})"
        params.extend(st)
    sql += " ORDER BY day DESC, briefing_id, seq"
    return pd.read_sql_query(sql, conn, params=params)


def name_to_code_map(conn: sqlite3.Connection) -> dict[str, str]:
    """종목명 → 코드. 공백을 제거해 '한화에어로 스페이스' 같은 표기 흔들림을 흡수한다."""
    out: dict[str, str] = {}
    for code, name in conn.execute("SELECT code, name FROM listing WHERE name IS NOT NULL"):
        out[name.replace(" ", "")] = code
    return out


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
    for t in (
        "ohlcv",
        "listing",
        "delisting",
        "flows",
        "indicators",
        "disclosures",
        "briefings",
        "briefing_views",
    ):
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
