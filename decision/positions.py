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


def _record_lot(
    conn: sqlite3.Connection,
    position_id: str,
    *,
    at: str,
    qty: int,
    avg_price: float,
    exit_price: int,
    reason: str,
) -> int:
    """판 몫 하나를 **실현손익 대장에 남기고** 그 손익을 돌려준다.

    포지션 행의 한 칸에만 담았더니 두 가지가 틀렸다.
      1. `close_position` 이 그 칸을 **덮어써서** 이전 TRIM 의 손익이 사라졌다
         (실측 2026-09-08: TRIM -25,056 → EXIT 뒤 +9,875 만 남았다)
      2. 일부 청산은 `closed_at` 이 없어 **날짜별 실현손익에 안 잡혔다** —
         일일 손실 한도가 그 값을 보므로 한도가 조용히 뚫린다
    """
    row = conn.execute(
        "SELECT arm, code FROM paper_positions WHERE position_id=?", (position_id,)
    ).fetchone()
    arm, code = row if row else (1, "")
    gross_buy = avg_price * qty * (1 + config.COMMISSION_RATE)
    gross_sell = exit_price * qty * (1 - config.COMMISSION_RATE - config.TAX_RATE)
    pnl = round(gross_sell - gross_buy)
    conn.execute(
        "INSERT INTO realized_lots (position_id,arm,code,at,qty,exit_price,reason,"
        "realized_pnl_krw) VALUES (?,?,?,?,?,?,?,?)",
        (position_id, arm, code, at[:10], qty, exit_price, reason, pnl),
    )
    return pnl


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
    pnl = _record_lot(
        conn,
        position_id,
        at=closed_at,
        qty=qty,
        avg_price=avg,
        exit_price=exit_price,
        reason=exit_reason,
    )
    # **덮어쓰지 않고 더한다.** 앞선 TRIM 의 손익이 여기서 사라졌다 (2026-09-08).
    prior = conn.execute(
        "SELECT COALESCE(realized_pnl_krw, 0) FROM paper_positions WHERE position_id=?",
        (position_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE paper_positions SET closed_at=?, exit_price=?, exit_reason=?, realized_pnl_krw=? "
        "WHERE position_id=?",
        (closed_at, exit_price, exit_reason, prior + pnl, position_id),
    )


def reduce_position(
    conn: sqlite3.Connection,
    position_id: str,
    *,
    qty: int,
    at: str,
    exit_price: int,
    exit_reason: str,
) -> int:
    """일부만 판다(TRIM). **평단은 바꾸지 않는다** — 판 몫의 손익만 확정한다.

    남은 수량이 0 이 되면 `close_position` 과 같은 결과여야 하므로 그쪽으로 넘긴다.
    두 경로가 손익을 각자 계산하면 전량 청산과 '전량만큼 축소'가 다른 숫자를 낸다.

    실현손익은 **누적**한다. 두 번 줄이면 두 번의 손익이 더해져야 한다 —
    덮어쓰면 첫 번째 축소가 없던 일이 된다.
    """
    row = conn.execute(
        "SELECT qty, avg_price, realized_pnl_krw FROM paper_positions "
        "WHERE position_id=? AND closed_at IS NULL",
        (position_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"열린 포지션이 아니다: {position_id}")
    held, avg, prior = row
    if qty <= 0 or qty > held:
        raise ValueError(f"{position_id}: 축소 수량 {qty} 가 보유 {held} 와 맞지 않는다")
    if qty == held:
        close_position(
            conn, position_id, closed_at=at, exit_price=exit_price, exit_reason=exit_reason
        )
        return 0

    pnl = _record_lot(
        conn,
        position_id,
        at=at,
        qty=qty,
        avg_price=avg,
        exit_price=exit_price,
        reason=exit_reason,
    )
    conn.execute(
        "UPDATE paper_positions SET qty=?, realized_pnl_krw=? WHERE position_id=?",
        (held - qty, (prior or 0) + pnl, position_id),
    )
    return held - qty


def open_qty(conn: sqlite3.Connection, code: str, arm: int) -> tuple[str | None, int]:
    """그 arm 이 이 종목을 몇 주 들고 있는가. **없으면 (None, 0)** 이다."""
    row = conn.execute(
        "SELECT position_id, qty FROM paper_positions "
        "WHERE closed_at IS NULL AND code=? AND arm=? LIMIT 1",
        (code, arm),
    ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


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
    """실현손익 누계. 수수료·거래세가 이미 반영돼 있다.

    **대장(`realized_lots`)에서 센다.** 예전에는 `closed_at IS NOT NULL` 인 포지션만
    셌는데, 그러면 **일부 청산(TRIM)한 손익이 계좌에 안 잡힌다** — 아직 열려 있으므로
    조건에 안 걸린다. 실측 2026-09-08: 우리금융지주 TRIM 의 -47,116 원이 빠져
    현금과 총자산이 그만큼 부풀어 있었다.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_krw), 0) FROM realized_lots WHERE arm = ?", (arm,)
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
    """그날 확정된 손익. **일일 손실 한도가 이 값을 본다.**

    포지션 행의 `closed_at` 으로 세면 **TRIM 은 날짜가 없어 안 잡힌다** — 한도가
    조용히 뚫린다. 대장은 판 몫마다 날짜를 갖는다.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_krw),0) FROM realized_lots WHERE at = ? AND arm = ?",
        (day.isoformat(), arm),
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
