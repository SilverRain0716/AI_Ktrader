"""지표 소급 — **측정하려면 표본이 있어야 한다.**

`task_indicators` 는 마지막 봉 하나만 계산했다. 그래서 지표가 5일치뿐이었고,
채널·규칙을 재려 해도 표본이 없었다 — 15일을 쟀다고 생각했는데 실제로는
같은 5일을 재사용한 것이었다(2026-09-07).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from data import pipeline as dp
from data import store


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO listing (code,name,market,is_preferred,is_spac,is_managed,market_cap,"
        "updated_at) VALUES ('005930','삼성전자','KOSPI',0,0,0,?,'2026-09-07')",
        (600_000_000_000_000,),
    )
    for i in range(160):
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        close = 70000 + i * 100
        conn.execute(
            "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted) "
            "VALUES ('005930',?,?,?,?,?,1000000,0,'t',1)",
            (d, close, close + 500, close - 500, close),
        )
    return conn


def _dates(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT date FROM indicators ORDER BY date")]


def test_여러_날짜에_지표를_남긴다(db):
    dp.task_indicators_backfill(db, days=10, limit=None)
    assert len(_dates(db)) == 10


def test_각_날짜는_그날까지의_봉만_본다(db):
    """미래 봉이 섞이면 **과거 재구성이 통째로 무의미해진다.**"""
    dp.task_indicators_backfill(db, days=5, limit=None)
    rows = db.execute("SELECT date, payload FROM indicators ORDER BY date").fetchall()
    bars = [json.loads(p)["bars"] for _d, p in rows]
    assert bars == sorted(bars), "봉 수가 날짜순으로 늘지 않는다"
    assert bars[-1] > bars[0]
    # 마지막 날의 종가가 그날 봉이어야 한다
    last_d, last_p = rows[-1]
    assert (
        json.loads(last_p)["indicators"]["close"]
        == db.execute(
            "SELECT close FROM ohlcv WHERE code='005930' AND date=?", (last_d,)
        ).fetchone()[0]
    )


def test_시가총액을_그날_주가로_환산한다(db):
    """오늘 시총을 과거에 그대로 대면 **최근 급등한 종목이 과거 필터를 통과한다** —
    하드 필터의 시총 하한이 미래 정보를 쓰게 된다.
    """
    dp.task_indicators_backfill(db, days=5, limit=None)
    rows = db.execute("SELECT date, payload FROM indicators ORDER BY date").fetchall()
    caps = [json.loads(p)["indicators"]["market_cap_eok_krw"] for _d, p in rows]
    assert caps[0] < caps[-1], "과거 시총이 오늘과 같다 — 환산이 안 됐다"
    # 마지막 날은 오늘 시총 그대로여야 한다 (환산 배수 1)
    assert caps[-1] == pytest.approx(6_000_000, rel=1e-6)


def test_두_번_돌려도_행이_늘지_않는다(db):
    """`(code, date)` 가 멱등키다 — 아니면 소급을 다시 돌릴 때마다 중복이 쌓인다."""
    dp.task_indicators_backfill(db, days=5, limit=None)
    n1 = db.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
    dp.task_indicators_backfill(db, days=5, limit=None)
    assert db.execute("SELECT COUNT(*) FROM indicators").fetchone()[0] == n1


def test_봉이_없는_종목은_건너뛴다(db):
    db.execute(
        "INSERT INTO listing (code,name,market,is_preferred,is_spac,is_managed,market_cap,"
        "updated_at) VALUES ('999999','없음','KOSPI',0,0,0,1000,'2026-09-07')"
    )
    dp.task_indicators_backfill(db, days=3, limit=None)
    assert db.execute("SELECT COUNT(*) FROM indicators WHERE code='999999'").fetchone()[0] == 0
