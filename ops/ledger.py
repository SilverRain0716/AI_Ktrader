"""매매 기록 — **주문 대장이 정본이고 포지션은 파생이다.**

    python -m ops.ledger                 # 전부
    python -m ops.ledger --arm 1
    python -m ops.ledger --since 2026-09-01

## 왜 두 곳을 함께 보는가

`paper_positions` 만 보면 **나가지 않은 주문이 안 보인다.** 차단됐거나 갭 가드에 걸렸거나
새 판단으로 폐기된 것들이 그렇다 — 그것들은 "매매 기록"에 없지만 **왜 없는지가 기록이다.**

실제로 09-01 이 그랬다: 판단은 열여섯 번 났고 접수는 두 건이었는데 포지션은 0 이다.
포지션만 보면 "아무 일도 없었다"로 읽히지만, 대장을 보면 **이월 금지 규칙이 두 건을
폐기했다**는 사실이 남아 있다.

## 손익은 저장된 값을 쓴다

`realized_pnl_krw` 는 청산 시점에 계산돼 저장된다. 여기서 다시 계산하지 않는다 —
수수료·세금 규칙이 바뀌면 과거 손익이 조용히 달라진다(누적 카운터를 두지 않는 원칙의 반대편).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

from data import store

log = logging.getLogger("ops.ledger")

# 주문이 체결로 이어지지 않은 상태들. **왜 안 나갔는지가 기록이다.**
UNFILLED = {
    "blocked": "게이트가 막음",
    "superseded": "새 판단으로 폐기 (이월 금지)",
    "gapped": "개장 갭이 허용 범위를 벗어남",
    "expired": "미체결 폐기",
    "rejected": "증권사가 거부",
    "failed": "주문 실패",
    "allowed": "판정만 되고 접수 전",
    "sent": "접수됨 · 체결 대기",
}


def trades(conn: sqlite3.Connection, *, arm: int | None = None, since: str | None = None):
    """체결된 매매. 청산된 것과 보유 중인 것을 함께."""
    where = ["1=1"]
    args: list = []
    if arm is not None:
        where.append("arm = ?")
        args.append(arm)
    if since:
        where.append("opened_at >= ?")
        args.append(since)
    return list(
        conn.execute(
            "SELECT arm, code, name, qty, avg_price, opened_at, closed_at, exit_price, "
            "exit_reason, realized_pnl_krw, invalidation_hit FROM paper_positions "
            f"WHERE {' AND '.join(where)} ORDER BY opened_at, arm",
            args,
        )
    )


def unfilled(conn: sqlite3.Connection, *, arm: int | None = None, since: str | None = None):
    """**나가지 않은 주문.** 왜 안 나갔는지가 기록이다."""
    where = ["status != 'filled'"]
    args: list = []
    if arm is not None:
        where.append("arm = ?")
        args.append(arm)
    if since:
        where.append("substr(created_at,1,10) >= ?")
        args.append(since)
    return list(
        conn.execute(
            "SELECT arm, code, action, qty, ref_price, status, reason, created_at "
            f"FROM order_intents WHERE {' AND '.join(where)} ORDER BY created_at, arm",
            args,
        )
    )


def _fmt(conn, rows_t, rows_u) -> None:
    names = dict(conn.execute("SELECT code,name FROM listing"))

    log.info("── 체결된 매매 (%d건)", len(rows_t))
    if not rows_t:
        log.info("   없음 — 아직 체결된 적이 없다")
    else:
        log.info(
            "   %-11s %-4s %-12s %5s %11s %11s %10s %s",
            "진입일",
            "arm",
            "종목",
            "수량",
            "진입가",
            "청산가",
            "손익",
            "사유",
        )
        for arm, code, name, qty, avg, opened, closed, exitp, reason, pnl, hit in rows_t:
            state = "보유중" if closed is None else (reason or "청산")
            pnl_s = "—" if pnl is None else f"{pnl:+,}"
            log.info(
                "   %-11s %-4d %-12s %5d %11s %11s %10s %s%s",
                opened[:10],
                arm,
                (name or names.get(code, code))[:11],
                qty,
                f"{avg:,}",
                f"{exitp:,}" if exitp else "—",
                pnl_s,
                state,
                "  [무효화 표시됨]" if hit else "",
            )

    log.info("── 나가지 않은 주문 (%d건)", len(rows_u))
    if not rows_u:
        log.info("   없음")
    for arm, code, action, qty, ref, status, reason, at in rows_u:
        why = UNFILLED.get(status, status)
        log.info(
            "   %s arm%d %-12s %-4s %4s주 @%-10s %-12s %s",
            at[5:16],
            arm,
            names.get(code, code)[:11],
            action,
            qty or "-",
            f"{ref:,}" if ref else "-",
            status,
            why,
        )
        if reason and reason[:20] not in why:
            log.info("        %s", reason[:96])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ops.ledger", description="매매 기록")
    p.add_argument("--arm", type=int, default=None, choices=[0, 1, 2])
    p.add_argument("--since", default=None, help="YYYY-MM-DD")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with store.connect() as conn:
        store.init_db(conn)
        t = trades(conn, arm=args.arm, since=args.since)
        u = unfilled(conn, arm=args.arm, since=args.since)
        _fmt(conn, t, u)
        if not t and not u:
            log.info("\n기록이 없다. 판단은 `python -m ops.report` 로 본다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
