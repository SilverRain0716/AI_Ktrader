"""실현손익 대장 — **판 몫마다 한 줄이고, 여기가 정본이다.**

포지션 행의 한 칸에만 담았더니 두 가지가 틀렸다 (2026-09-08 실측).
  1. `close_position` 이 그 칸을 **덮어써서** 이전 TRIM 의 손익이 사라졌다
     (TRIM -25,056 → EXIT 뒤 +9,875 만 남았다)
  2. 일부 청산은 `closed_at` 이 없어 **날짜별 실현손익에 안 잡혔다** —
     일일 손실 한도가 그 값을 보므로 한도가 조용히 뚫린다
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from data import store
from decision import positions as P

D2, D3 = "2026-09-02", "2026-09-03"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    P.open_position(
        conn=conn,
        position_id="p",
        arm=1,
        code="A",
        name="A",
        qty=100,
        avg_price=1000,
        opened_at="2026-09-01",
    )
    return conn


def _pos_cell(conn) -> int:
    return conn.execute("SELECT realized_pnl_krw FROM paper_positions").fetchone()[0]


def test_청산이_앞선_축소를_덮지_않는다(db):
    """실측: TRIM 손실 -25,056 이 EXIT 이익 +9,875 로 **바뀌어 있었다.**"""
    P.reduce_position(db, "p", qty=50, at=D2, exit_price=500, exit_reason="TRIM")
    trim = _pos_cell(db)
    assert trim < 0

    P.close_position(db, "p", closed_at=D3, exit_price=1200, exit_reason="EXIT")
    assert _pos_cell(db) != trim, "덮어썼다"
    # 누계는 두 몫의 합이어야 한다
    assert _pos_cell(db) == P.realized_pnl_total(db, 1)
    assert P.realized_pnl_total(db, 1) < 0  # -25,056 + 9,875


def test_열린_포지션의_실현손익도_계좌에_잡힌다(db):
    """`closed_at IS NOT NULL` 로 세면 TRIM 몫이 빠져 **현금과 총자산이 부풀어 오른다.**
    실측: 우리금융지주 TRIM 의 -47,116 원이 계좌에 없었다.
    """
    P.reduce_position(db, "p", qty=50, at=D2, exit_price=500, exit_reason="TRIM")
    assert (
        db.execute("SELECT COUNT(*) FROM paper_positions WHERE closed_at IS NULL").fetchone()[0]
        == 1
    )
    assert P.realized_pnl_total(db, 1) < 0, "열린 포지션의 실현손익이 안 잡힌다"


def test_날짜별로_갈린다(db):
    """일부 청산은 `closed_at` 이 없다 — 포지션 행으로는 날짜를 알 수 없다."""
    P.reduce_position(db, "p", qty=50, at=D2, exit_price=500, exit_reason="TRIM")
    P.close_position(db, "p", closed_at=D3, exit_price=1200, exit_reason="EXIT")
    a = P.realized_pnl_on(db, dt.date(2026, 9, 2), 1)
    b = P.realized_pnl_on(db, dt.date(2026, 9, 3), 1)
    assert a < 0 < b
    assert a + b == P.realized_pnl_total(db, 1)


def test_arm_마다_따로_센다(db):
    P.open_position(
        conn=db,
        position_id="q",
        arm=2,
        code="B",
        name="B",
        qty=10,
        avg_price=1000,
        opened_at="2026-09-01",
    )
    P.close_position(db, "p", closed_at=D3, exit_price=500, exit_reason="EXIT")
    P.close_position(db, "q", closed_at=D3, exit_price=1500, exit_reason="EXIT")
    assert P.realized_pnl_total(db, 1) < 0 < P.realized_pnl_total(db, 2)


def test_대장이_판_몫마다_한_줄이다(db):
    P.reduce_position(db, "p", qty=30, at=D2, exit_price=900, exit_reason="TRIM")
    P.reduce_position(db, "p", qty=30, at=D2, exit_price=900, exit_reason="TRIM")
    P.close_position(db, "p", closed_at=D3, exit_price=900, exit_reason="EXIT")
    rows = db.execute("SELECT qty, at, reason FROM realized_lots ORDER BY lot_id").fetchall()
    assert [r[0] for r in rows] == [30, 30, 40]
    assert [r[2] for r in rows] == ["TRIM", "TRIM", "EXIT"]


def test_손익은_다시_계산하지_않는다(db):
    """저장된 값을 쓴다 — 다시 계산하면 수수료·세금 규칙이 바뀔 때 과거가 달라진다."""
    P.close_position(db, "p", closed_at=D3, exit_price=900, exit_reason="EXIT")
    db.execute("UPDATE realized_lots SET realized_pnl_krw = 12345")
    assert P.realized_pnl_total(db, 1) == 12345
