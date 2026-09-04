"""거래량 폭증 측정 — **프롬프트가 인용하는 숫자는 재현 가능해야 한다.**

값 자체를 고정하지 않는다. 봉이 쌓이면 숫자는 바뀐다. 대신 **결론의 방향**을 지킨다 —
방향이 뒤집히면 프롬프트의 표가 틀린 것이므로 사람이 봐야 한다.
"""

from __future__ import annotations

import sqlite3
import statistics as st

import pytest

from data import store
from scripts import measure_volume_spike as m


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    return conn


def _bars(conn, code, rows):
    for d, close, vol in rows:
        conn.execute(
            "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted) "
            "VALUES (?,?,?,?,?,?,?,0,'t',1)",
            (code, d, close, close, close, close, vol),
        )


def _day(i: int) -> str:
    return f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"


def test_거래정지일은_세지_않는다(db):
    """`open=0·volume=0` 행이 들어가면 평균 거래량이 눌려 **가짜 폭증**이 된다."""
    _bars(db, "A", [(_day(i), 1000, 100) for i in range(30)])
    db.execute(
        "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted) "
        "VALUES ('A','2026-03-01',0,0,0,1000,0,1,'t',1)"
    )
    assert all(d != "2026-03-01" for d, _, _ in m._load(db)["A"])


def test_급등_폭증일을_찾아낸다(db):
    """25일 평온 → 하루 거래량 5배 + 8% 급등 → 그날이 버킷에 잡혀야 한다."""
    rows = [(_day(i), 1000, 100) for i in range(25)]
    rows.append((_day(25), 1080, 500))  # +8%, 거래량 5배
    rows += [(_day(26 + i), 1080, 100) for i in range(25)]
    _bars(db, "A", rows)
    _, buckets = m.measure(db)
    assert buckets["거래량 3배+ · 급등(+5%↑)"], "폭증 급등일을 못 찾았다"
    assert buckets["거래량 5배+ · 제자리 ★물량소화"] == {}


def test_물량소화는_가격이_안_움직인_날이다(db):
    """거래량만 터지고 가격이 제자리면 **누군가 그 물량을 다 받아낸 것**이다."""
    rows = [(_day(i), 1000, 100) for i in range(25)]
    rows.append((_day(25), 1005, 600))  # +0.5%, 거래량 6배
    rows += [(_day(26 + i), 1005, 100) for i in range(25)]
    _bars(db, "A", rows)
    _, buckets = m.measure(db)
    assert buckets["거래량 5배+ · 제자리 ★물량소화"]
    assert buckets["거래량 3배+ · 급등(+5%↑)"] == {}


@pytest.mark.skipif(True, reason="실제 창고가 필요하다 — 손으로 돌린다")
def test_실측_결론의_방향() -> None:
    """급등 폭증은 기준선보다 나쁘고, 완만한 자리는 낫다. **방향이 뒤집히면 프롬프트가 틀렸다.**"""
    with store.connect() as conn:
        base, buckets = m.measure(conn)
    h = 20
    assert st.median(buckets["거래량 3배+ · 급등(+5%↑)"][h]) < st.median(base[h])
    assert st.median(buckets["거래량 완만 · 완만상승"][h]) > st.median(base[h])
