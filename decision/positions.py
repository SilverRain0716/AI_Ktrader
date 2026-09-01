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
    arm: int = 1,
) -> None:
    """페이퍼 포지션을 연다.

    `arm` 은 **어느 가상 계좌인가**다 — 0=정량 / 1=브리핑 포함 / 2=브리핑 제외.
    섞으면 Arm 1 의 매수가 Arm 2 의 현금을 깎아 3-arm 대응비교가 무너진다.
    """
    conn.execute(
        """INSERT INTO paper_positions
           (position_id,arm,code,name,qty,avg_price,opened_at,entry_decision_id,entry_thesis,
            invalidation,invalidation_hit,stop_price,target_price,max_hold_days)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (
            position_id,
            arm,
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


def load_open(
    conn: sqlite3.Connection, as_of: date, total_equity_krw: int, arm: int = 1
) -> list[dict]:
    """열린 포지션을 컨텍스트 팩 형식으로. 파생값은 전부 여기서 재계산한다."""
    rows = conn.execute(
        """SELECT position_id,code,name,qty,avg_price,opened_at,entry_decision_id,entry_thesis,
                  invalidation,invalidation_hit,stop_price,target_price,max_hold_days
           FROM paper_positions WHERE closed_at IS NULL AND arm = ? ORDER BY opened_at""",
        (arm,),
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


def blocked_codes_on(conn: sqlite3.Connection, day: date, arm: int = 1) -> list[str]:
    """당일 손실로 청산한 종목. 같은 날 재진입을 막는다.

    빈 배열로 하드코딩돼 있었다 — 아침에 손절한 종목을 점심 사이클에서 다시 사도
    아무것도 막지 않았다 (점검 2026-08-23).
    """
    rows = conn.execute(
        "SELECT DISTINCT code FROM paper_positions "
        "WHERE closed_at IS NOT NULL AND substr(closed_at,1,10)=? "
        "AND COALESCE(realized_pnl_krw,0) < 0 AND arm = ? ORDER BY code",
        (day.isoformat(), arm),
    ).fetchall()
    return [r[0] for r in rows]


def cost_basis(conn: sqlite3.Connection, arm: int = 1) -> int:
    """열린 포지션의 **취득원가** 합계 (매수 수수료 포함).

    평가금이 아니라 원가다. 현금을 구할 때 평가금을 빼면 손익이 현금으로 둔갑한다 —
    포지션이 -30% 나면 현금이 30% 늘어나 물타기를 구조적으로 유도하게 된다
    (점검 2026-08-23 치명 A).
    """
    total = 0.0
    for qty, avg in conn.execute(
        "SELECT qty, avg_price FROM paper_positions WHERE closed_at IS NULL AND arm = ?",
        (arm,),
    ):
        total += avg * qty * (1 + config.COMMISSION_RATE)
    return round(total)


def realized_pnl_total(conn: sqlite3.Connection, arm: int = 1) -> int:
    """청산 완료된 포지션의 실현손익 누계. 수수료·거래세가 이미 반영돼 있다."""
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_krw), 0) FROM paper_positions "
        "WHERE closed_at IS NOT NULL AND arm = ?",
        (arm,),
    ).fetchone()
    return int(row[0]) if row else 0


def account_state(conn: sqlite3.Connection, seed_krw: int, arm: int = 1) -> dict:
    """계좌 상태를 회계 항등식으로 계산한다. **arm 마다 독립이다.**

    하나로 합치면 Arm 1 의 매수가 Arm 2 의 현금·비중·섹터 한도를 깎아 서로 간섭하고,
    `Arm1 − Arm2`(F3)·`Arm2 − Arm0`(F2)를 잴 수 없다 — 3-arm 대응비교의 전제가 무너진다.
    ADR 0005 는 차이를 재는 법만 정하고 계좌 분리를 적지 않았다(2026-09-01 발견).

        cash        = 시드 − Σ취득원가 + Σ실현손익
        total_equity = cash + Σ평가금

    `total_equity` 를 상수로 두면 실현손실이 계좌에서 사라지고, 포지션 사이징·비중·
    섹터 한도가 전부 존재하지 않는 자산 위에서 계산된다.
    """
    cost = cost_basis(conn, arm)
    realized = realized_pnl_total(conn, arm)
    holdings = holdings_value(conn, arm)
    cash = seed_krw - cost + realized
    return {
        "cash_available_krw": cash,
        "holdings_value_krw": holdings,
        "total_equity_krw": cash + holdings,
        "realized_pnl_total_krw": realized,
    }


def holdings_value(conn: sqlite3.Connection, arm: int = 1) -> int:
    total = 0
    for code, qty, avg in conn.execute(
        "SELECT code, qty, avg_price FROM paper_positions WHERE closed_at IS NULL AND arm = ?",
        (arm,),
    ):
        cur, _ = _last_close(conn, code)
        total += (cur or avg) * qty
    return total


def realized_pnl_on(conn: sqlite3.Connection, day: date, arm: int = 1) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_krw),0) FROM paper_positions "
        "WHERE closed_at LIKE ? AND arm = ?",
        (f"{day.isoformat()}%", arm),
    ).fetchone()
    return int(row[0]) if row else 0


def unrealized_pnl(conn: sqlite3.Connection, arm: int = 1) -> int:
    """평가손익. **순액 기준** — 지금 청산하면 손에 남는 금액이다.

    이 모듈의 `net_yield_pct` 는 수수료·거래세를 빼는데 여기서만 총액을 쓰면
    같은 `account` 블록 안에 순/총이 섞인다. 그 혼용이 K-Trader 백테스트의 승률을
    부풀린 원인이었다 (모듈 docstring 참조).
    """
    total = 0.0
    for code, qty, avg in conn.execute(
        "SELECT code, qty, avg_price FROM paper_positions WHERE closed_at IS NULL AND arm = ?",
        (arm,),
    ):
        cur, _ = _last_close(conn, code)
        if cur:
            buy_cost = avg * qty * (1 + config.COMMISSION_RATE)
            sell_net = cur * qty * (1 - config.COMMISSION_RATE - config.TAX_RATE)
            total += sell_net - buy_cost
    return round(total)


def now_kst_iso() -> str:
    from data import config as dcfg

    return datetime.now(dcfg.KST).isoformat(timespec="seconds")
