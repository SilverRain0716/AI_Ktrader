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
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from data import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 18

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
    sector       TEXT,          -- 한국표준산업분류 원문 (158종)
    sector_group TEXT,          -- 업종 대분류. 섹터 집중도 한도는 이걸로 판정한다
    industry     TEXT,
    dept         TEXT,          -- 코스닥 소속부. **KOSPI 는 전부 비어 있다**
    is_managed   INTEGER NOT NULL DEFAULT 0,
    is_managed_known INTEGER NOT NULL DEFAULT 0,  -- 판정을 실제로 했는가. 0이면 '모른다'
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
    disqualifying INTEGER NOT NULL DEFAULT 0,  -- 유니버스 영구 배제 사유인가 (방향까지 본 판정)
    resolving     INTEGER NOT NULL DEFAULT 0,  -- 배제 사유를 푸는 공시인가
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
-- **arm 마다 독립된 가상 계좌다.** 하나로 합치면 Arm 1 의 매수가 Arm 2 의 현금·비중·
-- 섹터 한도를 깎아 서로 간섭하고, `Arm1 − Arm2`(F3)·`Arm2 − Arm0`(F2)를 잴 수 없다.
-- ADR 0005 는 세 arm 의 차이를 재는 법만 정하고 계좌 분리를 적지 않았다 — 그 구멍이다.
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id       TEXT PRIMARY KEY,
    arm               INTEGER NOT NULL DEFAULT 1,  -- 0=정량 / 1=브리핑 포함 / 2=브리핑 제외
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

-- ── 결정 (append-only, 감사 추적) ───────────────────────
-- ADR 0007 결정 3. INSERT 만 존재한다 — UPDATE·DELETE 경로를 만들지 않고
-- 그 부재를 테스트로 고정한다. 잘못된 행도 영구히 남고, 정정은 새 행으로 한다.
--
-- rendered_input 을 통째로 저장하는 이유가 R6 의 교훈이다. 프롬프트 해시와
-- 렌더러 버전만 있으면 이론상 재구성이 되지만, 재구성 코드가 돌아가면서 다른 것을
-- 만들어도 알 수 없다. 실제 보낸 바이트가 있어야 재현 검사가 성립한다.
--
-- raw_response 를 payload 와 따로 두는 이유는 11.6 이다 — 파서가 근거 든 쪽을
-- 버렸을 때 원문이 남아 있어 오진을 뒤집을 수 있었다.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id    TEXT NOT NULL,       -- (pack_id, arm) 당 하나. 재시도가 재사용한다
    attempt        INTEGER NOT NULL,    -- 1부터. 재시도도 전부 남는다
    pack_id        TEXT NOT NULL,
    pack_sha256    TEXT NOT NULL,       -- 팩이 덮였는지 사후 검출용
    arm            INTEGER NOT NULL,    -- 0=정량 / 1=브리핑 포함 / 2=브리핑 제외
    -- 'live' = 집행 대상 (pack_id·arm 당 하나). 'experiment' = 비교·측정용.
    -- **실행 계층은 live 만 조회한다** — 섞이면 실험 결정이 주문으로 나간다.
    run_kind       TEXT NOT NULL DEFAULT 'live',
    provider       TEXT,                -- anthropic|openai. 교체는 모델 교체와 같은 급의 함수 변경이다
    cycle          TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    valid_until    TEXT NOT NULL,       -- 이 시각 이후 집행 금지
    model          TEXT,                -- arm 0 은 NULL
    prompt_id      TEXT,
    prompt_sha256  TEXT,
    render_version TEXT NOT NULL,
    api_params     TEXT,                -- JSON. 같은 프롬프트라도 effort 가 다르면 다른 함수다
    rendered_input TEXT,                -- 모델에 실제로 보낸 입력 전문
    raw_response   TEXT,                -- 모델 원문. 파싱 전
    payload        TEXT,                -- 계약을 통과한 결정 JSON. 실패면 NULL
    status         TEXT NOT NULL,       -- ok|abstain|schema_rejected|contract_rejected
                                        -- |api_error|timeout|expired
    problems       TEXT,                -- JSON 배열. 거부 사유
    monitorable    INTEGER,             -- 감시 가능한 invalidation 건수
    unmonitorable  INTEGER,             -- 감시 불가 건수. 높아지면 enum 을 재검토한다
    request_id     TEXT,
    input_tokens   INTEGER,
    -- 입력 중 캐시로 싸게 온 몫. **NULL 은 "모른다"이고 0 과 다르다** —
    -- 0 으로 두면 캐시가 안 먹은 것과 재지 못한 것이 구분되지 않는다.
    cached_input_tokens INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER,
    PRIMARY KEY (decision_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_dec_pack ON decisions(pack_id, arm);
CREATE INDEX IF NOT EXISTS idx_dec_time ON decisions(generated_at, arm);

-- 배치 실행 기록. 어떤 날 무엇이 실패했는지 남지 않으면 결손을 발견할 수 없다.
-- ── 타법인 출자현황 (ADR 0012) ──────────────────────────
-- **자회사 사전을 뉴스 제목에서 쌓지 않고 DART 에서 받아온다.**
-- 처음에 "쌓인 제목에서 빈도로 뽑는다"고 설계했는데 순환 논리였다 — 깔때기가
-- 종목명 있는 제목만 통과시키면 계열사명은 영원히 쌓이지 않는다.
--
-- 이 표는 **깔때기에 쓰지 않는다.** AI 가 subject 를 뽑은 뒤 그 이름 하나를
-- 해당 종목의 자회사(평균 62개) 안에서만 조회한다 — 4만 건으로 제목을 훑지 않으므로
-- 오탐이 구조적으로 생기지 않는다.
CREATE TABLE IF NOT EXISTS affiliates (
    corp_code  TEXT NOT NULL,          -- 모회사 DART 고유번호
    code       TEXT,                   -- 모회사 6자리 종목코드
    inv_prm    TEXT NOT NULL,          -- 피출자 법인명 (DART 표기 그대로)
    quota_rt   REAL,                   -- 기말 지분율 %. 못 읽으면 NULL — 0 이 아니다
    bsns_year  TEXT NOT NULL,          -- 어느 사업연도 보고서인가 (지분율은 시점 값이다)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (corp_code, inv_prm, bsns_year)
);
CREATE INDEX IF NOT EXISTS idx_affil_code ON affiliates(code);

-- ── 증거금 등급 (ADR 0013 원칙 1) ───────────────────────
-- **증권사가 종목별로 정하는 값이라 시세 API 에 없다.** 키움 ka10001 45개 필드에 없고
-- (crd_rt 는 신용비율이지 증거금률이 아니다), 네이버 종목 3개 탭에 '증거금' 0회다.
-- 사람이 HTS 에서 내려받은 CSV 를 적재한다.
--
-- **시총 프록시로 대신하면 안 된다.** 실측(182종목): 시총 3,000억 이상으로 거르면
-- 정밀도 90.1% 다 — 10종목 중 1개가 증거금 50~100% 종목이고, 그것이 원칙 1 이
-- 배제하려던 바로 그 종목이다. 삼천당제약은 시총 4조인데 증100% 다.
-- **스냅샷 이력이다.** 등급은 과거를 받아올 수 없는 소멸 원천이라(뉴스와 같은 이유)
-- 덮어쓰지 않고 as_of 별로 쌓는다. 현재 등급은 최신 as_of 를 읽는다.
CREATE TABLE IF NOT EXISTS margin_grades (
    code        TEXT NOT NULL,
    margin_pct  INTEGER NOT NULL,      -- 위탁증거금률 20/30/40/50/60/100
    name        TEXT,
    grade_raw   TEXT NOT NULL,         -- 원문 구분. 파싱이 틀렸을 때 되짚을 근거
    credit      TEXT,                  -- 신용 등급 A~E. 없으면 NULL (신용거래 불가)
    collateral  TEXT,                  -- 담보 등급
    short_sell  TEXT,                  -- 대주 등급
    halted      INTEGER NOT NULL DEFAULT 0,  -- '정지'
    caution     INTEGER NOT NULL DEFAULT 0,  -- '주의'·'경예' 등 시장경보
    as_of       TEXT NOT NULL,         -- 내려받은 날. 등급은 수시로 바뀐다
    PRIMARY KEY (code, as_of)
);
CREATE INDEX IF NOT EXISTS idx_margin_asof ON margin_grades(as_of, margin_pct);

-- ── 주문 의도 대장 (집행 게이트) ────────────────────────
-- **주문이 나가기 전에 여기 먼저 남는다.** 어댑터가 응답을 못 줘도(타임아웃·연결 끊김)
-- 의도는 남아 있어야 재시도가 중복 주문이 되지 않는다.
--
-- `decision_id` 가 UNIQUE 다 — 같은 결정으로 두 번 주문하지 않는다는 뜻이다.
-- 멱등키는 모델이 아니라 러너가 만든다(ADR 0007).
CREATE TABLE IF NOT EXISTS order_intents (
    intent_id    TEXT PRIMARY KEY,      -- decision_id + 종목. 결정 하나가 여러 종목을 낸다
    decision_id  TEXT NOT NULL,
    code         TEXT NOT NULL,
    action       TEXT NOT NULL,         -- BUY|ADD|TRIM|EXIT
    qty          INTEGER,
    -- 세 가격을 나눈다. 하나로 뭉치면 "무엇을 지시했고 무엇에 체결됐나"를 되짚을 수 없다.
    limit_price  INTEGER,               -- 지시한 지정가. NULL 이면 시장가
    ref_price    INTEGER,               -- 수량을 정할 때 쓴 기준가 (지정가 또는 전일 종가)
    fill_price   INTEGER,               -- 실제 체결가
    mode         TEXT NOT NULL,         -- paper|mock|live. 어느 모드에서 만들어졌는가
    kiwoom_env   TEXT NOT NULL,         -- real|mock. 어느 서버를 향했는가
    created_at   TEXT NOT NULL,
    -- 집행 결과. 게이트는 여기까지 채우지 않는다 — 어댑터가 생기면 채운다.
    status       TEXT NOT NULL,         -- blocked|allowed|sent|filled|rejected|failed
    reason       TEXT,                  -- 차단·실패 사유
    broker_ref   TEXT,                  -- 증권사 주문번호
    -- 어느 가상 계좌의 주문인가. **arm 마다 다른 계좌를 쓴다**(ADR 0014) —
    -- 섞이면 두 계좌가 동시에 오염되고 `Arm1 − Arm2` 를 영영 못 잰다.
    arm          INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_dec_code
    ON order_intents(decision_id, code);
CREATE INDEX IF NOT EXISTS idx_intent_time ON order_intents(created_at);

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


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN 을 멱등하게. 이미 있으면 아무것도 하지 않는다.

    _SCHEMA 는 CREATE TABLE IF NOT EXISTS 라 새로 만드는 DB에는 컬럼이 이미 들어 있다.
    구버전 DB에만 ALTER 가 필요하므로 무조건 실행하면 duplicate column 으로 깨진다.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "listing", "sector_group", "TEXT")
    _add_column_if_missing(conn, "listing", "dept", "TEXT")
    _add_column_if_missing(conn, "listing", "is_managed", "INTEGER NOT NULL DEFAULT 0")


# 전진 마이그레이션. 키는 "도달할 버전", 값은 그 버전으로 올리는 작업.
# _SCHEMA 는 전부 IF NOT EXISTS 라 새 테이블 추가는 재실행만으로 반영된다.
# ALTER 가 필요한 변경만 여기에 함수로 적는다.
def _migrate_v6(conn):
    """공시에 '배제 사유인가' 판정을 붙인다.

    카테고리만으로 배제하면 `불성실공시법인미지정` 같은 해소 공시가 악재로 뒤집힌다.
    이미 적재된 행도 다시 판정한다 — 판정 규칙이 바뀌었는데 옛 행을 그대로 두면
    같은 DB 안에서 기준이 두 개가 된다.
    """
    from data.sources import dart

    _add_column_if_missing(conn, "disclosures", "disqualifying", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "disclosures", "resolving", "INTEGER NOT NULL DEFAULT 0")
    rows = conn.execute("SELECT rcept_no, report_nm, category FROM disclosures").fetchall()
    conn.executemany(
        "UPDATE disclosures SET disqualifying=?, resolving=? WHERE rcept_no=?",
        [
            (int(dart.is_disqualifying(nm, cat)), int(dart.is_resolving(nm, cat)), no)
            for no, nm, cat in rows
        ],
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_disq ON disclosures(disqualifying, code)")


def _migrate_v7(conn):
    """업종 대분류를 다시 매긴다.

    분류 규칙이 넓어졌는데 이미 적재된 행을 그대로 두면 같은 DB 안에서 기준이 두 개가 된다.
    industry 원문은 이미 있으므로 재적재 없이 되돌릴 수 있다.
    """
    from data.sources.listing import sector_group

    rows = conn.execute("SELECT code, industry FROM listing").fetchall()
    conn.executemany(
        "UPDATE listing SET sector_group=? WHERE code=?",
        [(sector_group(ind), code) for code, ind in rows],
    )


def _migrate_v8(conn):
    """관리종목 '판정 여부'를 분리한다.

    is_managed=0 이 "관리종목이 아니다"와 "판정하지 못했다"를 겸하고 있었다.
    FDR 소속부가 코스닥에만 있어 KOSPI 942종목이 전부 후자였는데 전자로 취급됐다.
    """
    _add_column_if_missing(conn, "listing", "is_managed_known", "INTEGER NOT NULL DEFAULT 0")
    # 기존 값 중 신뢰할 수 있는 것은 소속부가 있던 종목(코스닥)뿐이다.
    conn.execute(
        "UPDATE listing SET is_managed_known = CASE "
        "WHEN dept IS NOT NULL AND TRIM(dept) != '' THEN 1 ELSE 0 END"
    )


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """decisions.provider 추가. 제공자는 판단 함수의 신원 일부다 (ADR 0007 동결 정책)."""
    _add_column_if_missing(conn, "decisions", "provider", "TEXT")


def _migrate_v13(conn):
    """`margin_grades` 를 스냅샷 이력으로 바꾼다.

    처음에 `code` 를 PK 로 두고 저장 때마다 전체를 지웠다. **틀렸다** — 증거금 등급은
    과거를 받아올 수 없는 소멸 원천이고([ADR 0013](../docs/adr/0013-trading-doctrine.md)),
    덮어쓰면 소급 검증이 영영 불가능해진다. 뉴스를 매일 쌓기로 한 것과 같은 이유다.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(margin_grades)")}
    if not cols:
        return
    pk = [r[1] for r in conn.execute("PRAGMA table_info(margin_grades)") if r[5]]
    if pk == ["code", "as_of"]:
        return
    conn.executescript("ALTER TABLE margin_grades RENAME TO margin_grades_old;")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO margin_grades "
        "(code,margin_pct,name,grade_raw,credit,collateral,short_sell,halted,caution,as_of) "
        "SELECT code,margin_pct,name,grade_raw,credit,collateral,short_sell,halted,caution,as_of "
        "FROM margin_grades_old"
    )
    conn.execute("DROP TABLE margin_grades_old")


def _migrate_v14(conn):
    """`decisions.run_kind` 를 추가한다.

    프롬프트 A/B 를 하려면 같은 팩을 여러 번 판단해야 하는데, `decision_id` 가
    `(pack_id, arm)` 결정론이라 UNIQUE 제약에 막혔다. **멱등키는 그대로 둔다** —
    같은 팩·arm 에 집행 대상이 둘이면 중복 주문 위험이 생긴다.

    대신 실험 결정에 접미사를 붙이고 `run_kind='experiment'` 로 표시한다.
    **실행 계층은 `run_kind='live'` 만 본다.**
    """
    _add_column_if_missing(conn, "decisions", "run_kind", "TEXT NOT NULL DEFAULT 'live'")


def _migrate_v16(conn):
    """`order_intents` 의 가격을 셋으로 나눈다.

    `limit_price` 하나가 지시한 지정가·수량 산출 기준가·실제 체결가를 뒤섞고 있었다
    (2026-09-01 실측: 접수 뒤 대장에 0 이 남고, 체결 시 지정가가 체결가로 덮였다).
    **대장은 감사 기록이다** — 무엇을 지시했고 무엇에 체결됐는지 되짚을 수 없으면 쓸모가 없다.
    """
    _add_column_if_missing(conn, "order_intents", "ref_price", "INTEGER")
    _add_column_if_missing(conn, "order_intents", "fill_price", "INTEGER")


def _migrate_v17(conn):
    """`paper_positions`·`order_intents` 에 arm 을 붙인다.

    **계좌가 하나면 3-arm 대응비교가 불가능하다**(2026-09-01 발견).
    `cash = 시드 − Σ취득원가 + Σ실현손익` 이 전체 합산이라 Arm 1 의 매수가 Arm 2 의
    여력을 깎았다. ADR 0005 는 차이를 재는 법만 정하고 계좌 분리를 적지 않았다.

    기존 행은 arm=1 로 둔다 — 지금까지의 기록은 브리핑 포함 판단에서 나왔다.
    """
    _add_column_if_missing(conn, "paper_positions", "arm", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "order_intents", "arm", "INTEGER NOT NULL DEFAULT 1")


def _migrate_v18(conn: sqlite3.Connection) -> None:
    """캐시 적중률을 재려면 칸이 있어야 한다. **기존 행은 NULL 로 둔다** —
    소급해 0 을 넣으면 "캐시가 안 먹었다"는 없는 측정이 생긴다."""
    _add_column_if_missing(conn, "decisions", "cached_input_tokens", "INTEGER")


_MIGRATIONS: dict[int, object] = {
    2: "",  # disclosures 테이블 추가 — _SCHEMA 재실행으로 충분
    3: "",  # briefings·briefing_views 추가 — _SCHEMA 재실행으로 충분
    4: "",  # paper_positions·context_packs 추가 — _SCHEMA 재실행으로 충분
    5: _migrate_v5,  # listing 컬럼 추가
    6: _migrate_v6,  # disclosures.disqualifying·resolving 추가 + 기존 행 재판정
    7: _migrate_v7,  # 업종 대분류 규칙 확장 → 기존 행 재분류
    8: _migrate_v8,  # is_managed_known 분리 (판정 못한 것을 정상으로 두지 않는다)
    9: "",  # decisions 테이블 추가 — _SCHEMA 재실행으로 충분
    10: _migrate_v10,  # decisions.provider 추가
    11: "",  # affiliates 추가 — _SCHEMA 재실행으로 충분 (ADR 0012)
    12: "",  # margin_grades 추가 — _SCHEMA 재실행으로 충분 (ADR 0013)
    13: _migrate_v13,  # margin_grades 를 스냅샷 이력으로 (PK 변경)
    14: _migrate_v14,  # decisions.run_kind — 실험 결정을 집행 대상과 가른다
    15: "",  # order_intents 추가 — _SCHEMA 재실행으로 충분 (집행 게이트)
    16: _migrate_v16,  # order_intents 의 가격을 지정가·기준가·체결가로 나눈다
    17: _migrate_v17,  # arm 별 독립 가상 계좌 (3-arm 대응비교)
    18: _migrate_v18,  # decisions.cached_input_tokens — 캐시 적중률을 잰다
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
        step = _MIGRATIONS[version]
        if callable(step):
            step(conn)
        elif isinstance(step, str) and step.strip():
            conn.executescript(step)
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
            r.sector_group,
            r.industry,
            r.dept,
            int(bool(r.is_managed)),
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
           (code,name,market,sector,sector_group,industry,dept,is_managed,listing_date,
            market_cap,shares,is_preferred,is_spac,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            int(bool(getattr(r, "disqualifying", False))),
            int(bool(getattr(r, "resolving", False))),
            r.filer,
            r.remark,
            r.url,
        )
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO disclosures
           (rcept_no,rcept_dt,corp_code,code,corp_name,corp_cls,
            report_nm,category,material,disqualifying,resolving,filer,remark,url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rcept_no) DO UPDATE SET
             report_nm=excluded.report_nm, category=excluded.category,
             material=excluded.material, disqualifying=excluded.disqualifying,
             resolving=excluded.resolving,
             remark=excluded.remark""",
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
    exclude_managed: bool = True,
    min_market_cap: float | None = None,
) -> list[str]:
    sql = "SELECT code FROM listing WHERE 1=1"
    params: list = []
    if exclude_preferred:
        sql += " AND is_preferred=0"
    if exclude_spac:
        sql += " AND is_spac=0"
    if exclude_managed:
        sql += " AND is_managed=0"
    if min_market_cap is not None:
        sql += " AND market_cap >= ?"
        params.append(min_market_cap)
    sql += " ORDER BY market_cap DESC"
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def managed_unknown_count(conn: sqlite3.Connection, *, min_market_cap: float) -> int:
    """관리종목 판정을 하지 못한 모집단 종목 수.

    0이 아니면 그만큼은 "관리종목이 아니다"가 아니라 "모른다"이다.
    이 구분을 팩에 싣지 않으면 판정 실패가 조용히 통과가 된다.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM listing WHERE is_preferred=0 AND is_spac=0 "
        "AND market_cap IS NOT NULL AND market_cap >= ? AND is_managed_known = 0",
        (min_market_cap,),
    ).fetchone()
    return int(row[0]) if row else 0


def universe_coverage(conn: sqlite3.Connection, *, min_market_cap: float) -> tuple[int, int]:
    """(모집단, 지표까지 확보된 종목 수).

    모집단은 "유니버스 후보가 될 자격이 있는 종목" — 시총 하한을 넘는 보통주 중
    우선주·스팩·관리종목·상장폐지·거래정지를 뺀 것이다. 커버리지는 그 중 지표를 계산해 둔 비율.

    하드 필터 통과 종목 수만 세면 절단된 모수 위에서 세는 것이라 아무것도 검증하지 못한다.
    값이 아니라 값이 나온 모수를 본다 (점검 2026-08-22 결함 2·4).
    """
    # 마지막 봉이 거래정지인 종목은 지표를 계산할 방법이 없다(유효봉 0). 모집단에 남기면
    # 커버리지가 영원히 100%에 못 미치고, 그 미달분이 무슨 뜻인지 아무도 기억하지 못하게 된다.
    # 이유를 잊은 채 통과하는 임계값이 애초에 이 결함의 정체였다.
    where = """
        FROM listing l
        WHERE l.is_preferred = 0 AND l.is_spac = 0 AND l.is_managed = 0
          AND l.market_cap IS NOT NULL AND l.market_cap >= ?
          AND l.code NOT IN (SELECT DISTINCT code FROM delisting)
          AND NOT EXISTS (
              SELECT 1 FROM ohlcv o
              WHERE o.code = l.code AND o.halted = 1
                AND o.date = (SELECT MAX(date) FROM ohlcv x WHERE x.code = l.code)
          )
    """
    expected = conn.execute(f"SELECT COUNT(*) {where}", (min_market_cap,)).fetchone()[0]
    covered = conn.execute(
        f"SELECT COUNT(*) {where} AND EXISTS (SELECT 1 FROM indicators i WHERE i.code = l.code)",
        (min_market_cap,),
    ).fetchone()[0]
    return expected, covered


def disclosure_span(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """공시를 언제부터 언제까지 적재했는가 (YYYYMMDD).

    악재공시 배제가 **영구**이므로, 배제 집합의 크기는 이 구간 길이에 정비례한다.
    구간을 밝히지 않으면 "이 종목은 왜 유니버스에 없나"에 답할 수 없고,
    적재를 늘릴 때마다 유니버스가 조용히 줄어든다.
    """
    row = conn.execute("SELECT MIN(rcept_dt), MAX(rcept_dt) FROM disclosures").fetchone()
    return (row[0], row[1]) if row else (None, None)


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


def upsert_affiliates(conn: sqlite3.Connection, rows: list[dict], code: str | None = None) -> int:
    """타법인 출자현황 적재 (ADR 0012). 같은 (모회사, 피출자, 연도) 는 덮어쓴다."""
    if not rows:
        return 0
    now = datetime.now(tz=UTC).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO affiliates "
        "(corp_code, code, inv_prm, quota_rt, bsns_year, fetched_at) VALUES (?,?,?,?,?,?)",
        [
            (r["corp_code"], code, r["inv_prm"], r.get("quota_rt"), r["bsns_year"], now)
            for r in rows
        ],
    )
    return len(rows)


def affiliates_of(conn: sqlite3.Connection, code: str) -> dict[str, float | None]:
    """그 종목의 자회사 `{DART 표기: 지분율 %}`. 최신 사업연도만 쓴다.

    **깔때기용이 아니다.** AI 가 뽑은 subject 하나를 여기서 조회할 뿐이다 —
    4만 건으로 제목을 훑지 않으므로 오탐이 구조적으로 생기지 않는다.
    """
    row = conn.execute("SELECT MAX(bsns_year) FROM affiliates WHERE code = ?", (code,)).fetchone()
    if not row or not row[0]:
        return {}
    return {
        name: rt
        for name, rt in conn.execute(
            "SELECT inv_prm, quota_rt FROM affiliates WHERE code = ? AND bsns_year = ?",
            (code, row[0]),
        )
    }
