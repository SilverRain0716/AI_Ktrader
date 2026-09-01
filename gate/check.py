"""집행 판정 — 이 결정으로 주문을 내도 되는가. **주문은 내지 않는다.**

## 차단 순서가 곧 우선순위다

1. **킬 스위치** — 다른 무엇보다 먼저다
2. **설정 모순** — 모의라면서 실전 서버를 향하는 것 등
3. **실험 결정** — `run_kind='experiment'` 는 절대 집행하지 않는다
4. **결정 상태** — `ok` 가 아니면(abstain·거부·장애) 집행할 것이 없다
5. **만료** — `valid_until` 을 넘겼으면 시장이 달라졌다
6. **중복** — 이미 주문 의도가 남아 있으면 두 번 내지 않는다

**막을 이유가 하나라도 있으면 전부 모아서 돌려준다.** 하나씩 고쳐가며 재실행하지 않아도
되게 하려는 것이고, `decision/config.py` 의 `missing_limits()` 와 같은 방식이다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from data import config as dcfg
from gate import config as gcfg


@dataclass(frozen=True)
class Verdict:
    decision_id: str
    allowed: bool
    mode: str
    blockers: tuple[str, ...] = ()
    orders: tuple[dict, ...] = field(default=())
    # 막지는 않지만 사람이 알아야 하는 것. **차단과 섞지 않는다** —
    # 섞으면 못 막을 것을 막거나 막아야 할 것을 흘린다.
    notes: tuple[str, ...] = ()

    @property
    def sends_orders(self) -> bool:
        """실제로 증권사에 나가는가. **paper 는 통과해도 주문이 없다.**"""
        return self.allowed and self.mode in (gcfg.MOCK, gcfg.LIVE)


# 주문이 **실제로 나간** 상태. `allowed` 는 판정만 된 것이지 아직 안 나갔다.
PLACED = ("sent", "filled", "gapped", "expired", "rejected", "failed")


def _already(conn: sqlite3.Connection, decision_id: str) -> set[str]:
    """이미 **주문이 나간** 종목. 멱등성이 보호하는 것은 중복 주문이다.

    처음에 `order_intents` 에 행이 있기만 하면 중복으로 봤다. **틀렸다** —
    `check --record` 가 판정을 남기면 그 뒤 `place` 가 항상 막혔다(2026-09-01 실측).
    기록됨과 주문됨은 다르다.
    """
    marks = ",".join("?" * len(PLACED))
    return {
        r[0]
        for r in conn.execute(
            f"SELECT code FROM order_intents WHERE decision_id = ? AND status IN ({marks})",
            (decision_id, *PLACED),
        )
    }


def evaluate(
    conn: sqlite3.Connection,
    decision_id: str,
    *,
    now: datetime | None = None,
    deposit_krw: int | None = None,
) -> Verdict:
    """한 결정에 대한 집행 판정. **아무것도 쓰지 않는다.**

    `deposit_krw` 는 증권사 계좌 예수금이다. `mock`/`live` 에서는 **반드시 있어야 한다** —
    시드가 잔고를 넘으면 주문이 거부되거나 미수가 남는다. 네트워크 호출을 이 함수 안에
    두지 않으려고 인자로 받는다 — **테스트가 실제 계좌를 치면 안 된다.**
    """
    now = now or datetime.now(dcfg.KST)
    m = gcfg.mode()
    blockers: list[str] = []
    notes: list[str] = []

    kill = gcfg.kill_switch()
    if kill.on:
        blockers.append(f"킬 스위치: {kill.reason}")
    blockers += gcfg.check_coherent()

    if m in (gcfg.MOCK, gcfg.LIVE):
        from decision import config as ccfg
        from gate import account as gacct

        bad, note = gacct.check_seed(ccfg.account_seed()["total_equity_krw"], deposit_krw)
        blockers += bad
        notes += note

    row = conn.execute(
        "SELECT run_kind, status, valid_until, payload FROM decisions "
        "WHERE decision_id = ? ORDER BY attempt DESC LIMIT 1",
        (decision_id,),
    ).fetchone()
    if row is None:
        return Verdict(
            decision_id, False, m, (*blockers, f"결정 {decision_id} 이 없다"), notes=tuple(notes)
        )

    run_kind, status, valid_until, payload = row
    if run_kind != "live":
        blockers.append(f"run_kind={run_kind} — 실험 결정은 집행하지 않는다")
    if status != "ok":
        blockers.append(f"status={status} — 집행할 결정이 아니다")
    if valid_until and now.isoformat() > valid_until:
        blockers.append(
            f"만료됨 (valid_until={valid_until}, 지금 {now.isoformat(timespec='seconds')})"
        )

    orders: list[dict] = []
    if payload:
        seen = _already(conn, decision_id)
        for d in json.loads(payload).get("decisions") or []:
            if d.get("action") not in ("BUY", "ADD", "TRIM", "EXIT"):
                continue  # HOLD 는 주문이 아니다
            if d["code"] in seen:
                blockers.append(f"{d['code']}: 이미 주문 의도가 남아 있다 — 중복 주문을 막는다")
                continue
            orders.append(
                {
                    "code": d["code"],
                    "action": d["action"],
                    "weight_pct": d.get("weight_pct"),
                    "entry": d.get("entry"),
                }
            )

    return Verdict(decision_id, not blockers, m, tuple(blockers), tuple(orders), tuple(notes))


def record(conn: sqlite3.Connection, v: Verdict, *, now: datetime | None = None) -> int:
    """판정을 대장에 남긴다. **주문은 내지 않는다** — 어댑터가 생기면 그것이 낸다.

    차단됐어도 남긴다. *"왜 그날 주문이 안 나갔는가"* 를 나중에 물을 수 있어야 한다.
    """
    now = now or datetime.now(dcfg.KST)
    env = gcfg.order_target()
    reason = "; ".join(v.blockers) or None
    rows = [
        (
            f"{v.decision_id}-{o['code']}",
            v.decision_id,
            o["code"],
            o["action"],
            None,
            (o.get("entry") or {}).get("price"),
            v.mode,
            env,
            now.isoformat(timespec="seconds"),
            "allowed" if v.allowed else "blocked",
            reason,
        )
        for o in v.orders
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO order_intents (intent_id,decision_id,code,action,qty,"
        "limit_price,mode,kiwoom_env,created_at,status,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


__all__ = ["Verdict", "evaluate", "record"]
