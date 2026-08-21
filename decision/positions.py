"""페이퍼 포지션.

실행 계층이 생기기 전까지 포지션의 정본이다.

**파생값은 저장하지 않는다.** 평가손익·보유일수·비중·고점은 조회 시 매번 재계산한다.
K-Trader가 누적 카운터 드리프트로 겪은 문제를 반복하지 않기 위해서다.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from decision import config


def net_yield_pct(buy_price: float, sell_price: float) -> float:
    """수수료·거래세를 뺀 순수익률(%).

    총수익률과 섞으면 익절 기준이 조용히 어긋난다 — K-Trader 백테스트가
    정확히 이 문제로 승률이 부풀려져 있었다.
    """
    if buy_price <= 0:
        return 0.0
    buy_cost = buy_price * (1 + config.COMMISSION_RATE)
    sell_net = sell_price * (1 - config.COMMISSION_RATE - config.TAX_RATE)
    return (sell_net - buy_cost) / buy_cost * 100


def open_position(
    conn: sqlite3.Connection,
    *,
    position_id: str,
    code: str,
    name: str | None,
    qty: int,
    avg_price: int,
    opened_at: str,
    entry_decision_id: str | None = None,
    entry_thesis: str | None = None,
    invalidation: str | None = None,
    stop_price: int | None = None,
    target_price: int | None = None,
    max_hold_days: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO paper_positions
           (position_id,code,name,qty,avg_price,opened_at,entry_decision_id,entry_thesis,
            invalidation,invalidation_hit,stop_price,target_price,max_hold_days)
           VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (
            position_id,
            code,
            name,
            qty,
            avg_price,
            opened_at,
            entry_decision_id,
            entry_thesis,
            invalidation,
            stop_price,
            target_price,
            max_hold_days,
        ),
    )


def close_position(
    conn: sqlite3.Connection, position_id: str, *, closed_at: str, exit_price: int, exit_reason: str
) -> None:
    row = conn.execute(
        "SELECT qty, avg_price FROM paper_positions WHERE position_id=? AND closed_at IS NULL",
        (position_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"열린 포지션이 아니다: {position_id}")
    qty, avg = row
    gross_buy = avg * qty * (1 + config.COMMISSION_RATE)
    gross_sell = exit_price * qty * (1 - config.COMMISSION_RATE - config.TAX_RATE)
    conn.execute(
        "UPDATE paper_positions SET closed_at=?, exit_price=?, exit_reason=?, realized_pnl_krw=? "
        "WHERE position_id=?",
        (closed_at, exit_price, exit_reason, round(gross_sell - gross_buy), position_id),
    )


def _last_close(conn: sqlite3.Connection, code: str) -> tuple[int | None, str | None]:
    row = conn.execute(
        "SELECT close, date FROM ohlcv WHERE code=? AND halted=0 AND volume>0 "
        "ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _high_since(conn: sqlite3.Connection, code: str, since: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(high) FROM ohlcv WHERE code=? AND date>=? AND halted=0", (code, since)
    ).fetchone()
    return row[0] if row and row[0] else None


def _trading_days_between(conn: sqlite3.Connection, code: str, start: str, end: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM ohlcv WHERE code=? AND date>? AND date<=? AND halted=0",
        (code, start, end),
    ).fetchone()
    return int(row[0]) if row else 0


def load_open(conn: sqlite3.Connection, as_of: date, total_equity_krw: int) -> list[dict]:
    """열린 포지션을 컨텍스트 팩 형식으로. 파생값은 전부 여기서 재계산한다."""
    rows = conn.execute(
        """SELECT position_id,code,name,qty,avg_price,opened_at,entry_decision_id,entry_thesis,
                  invalidation,invalidation_hit,stop_price,target_price,max_hold_days
           FROM paper_positions WHERE closed_at IS NULL ORDER BY opened_at""",
    ).fetchall()

    out: list[dict] = []
    for (
        _pid,
        code,
        name,
        qty,
        avg,
        opened_at,
        dec_id,
        thesis,
        inval,
        inval_hit,
        stop,
        target,
        max_days,
    ) in rows:
        cur, _ = _last_close(conn, code)
        if cur is None:
            cur = avg  # 시세가 없으면 평단으로 둔다. 손익 0으로 보이지만 지어내지는 않는다
        opened_day = opened_at[:10]
        out.append(
            {
                "code": code,
                "name": name,
                "qty": qty,
                "avg_price": avg,
                "current_price": cur,
                "net_yield_pct": round(net_yield_pct(avg, cur), 2),
                "high_since_entry": _high_since(conn, code, opened_day) or cur,
                "weight_pct": round(cur * qty / total_equity_krw * 100, 2)
                if total_equity_krw
                else 0.0,
                "held_days": _trading_days_between(conn, code, opened_day, as_of.isoformat()),
                "entry_decision_id": dec_id,
                "entry_thesis": thesis,
                "invalidation": inval,
                "invalidation_hit": bool(inval_hit),
                "stop_price": stop,
                "target_price": target,
                "max_hold_days": max_days,
                "indicators": _indicators(conn, code),
            }
        )
    return out


def _indicators(conn: sqlite3.Connection, code: str) -> dict:
    import json

    row = conn.execute(
        "SELECT payload FROM indicators WHERE code=? ORDER BY date DESC LIMIT 1", (code,)
    ).fetchone()
    if not row:
        return {}
    try:
        return (json.loads(row[0]) or {}).get("indicators") or {}
    except json.JSONDecodeError:
        return {}


def holdings_value(conn: sqlite3.Connection) -> int:
    total = 0
    for code, qty, avg in conn.execute(
        "SELECT code, qty, avg_price FROM paper_positions WHERE closed_at IS NULL"
    ):
        cur, _ = _last_close(conn, code)
        total += (cur or avg) * qty
    return total


def realized_pnl_on(conn: sqlite3.Connection, day: date) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_krw),0) FROM paper_positions WHERE closed_at LIKE ?",
        (f"{day.isoformat()}%",),
    ).fetchone()
    return int(row[0]) if row else 0


def unrealized_pnl(conn: sqlite3.Connection) -> int:
    total = 0
    for code, qty, avg in conn.execute(
        "SELECT code, qty, avg_price FROM paper_positions WHERE closed_at IS NULL"
    ):
        cur, _ = _last_close(conn, code)
        if cur:
            total += (cur - avg) * qty
    return int(total)


def now_kst_iso() -> str:
    from data import config as dcfg

    return datetime.now(dcfg.KST).isoformat(timespec="seconds")
