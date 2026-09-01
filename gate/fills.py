"""체결 확인 — **증권사에 물어서 채운다. 추정하지 않는다.**

## 왜 시뮬레이터와 다른 코드인가

`SimBroker.settle()` 은 일봉으로 체결을 **추정**한다. 시장가는 시가에, 지정가는
저가가 닿았으면 체결됐다고 본다. 시뮬레이터에서는 그것이 최선이다.

**실주문은 추정이 아니라 조회다.** 부분체결·거부·정정이 실제로 일어나고,
평균단가는 우리가 계산할 수 없다. 그래서 `ka10076`(체결요청)을 물어서 채운다.

## 주문이 나가기 전에 만든다

[CLAUDE.md](../CLAUDE.md) 의 순서 원칙이다 — *"실행 계층에서 주문만 제외한 것을 먼저
만들고, 3개월 돌린 뒤, 주문 어댑터만 교체한다."* 체결 조회는 **읽기 전용**이라
`execution/` 없이 지금 만들 수 있고, 어댑터가 붙는 날 이미 검증돼 있어야 한다.

**주문만 있고 체결 확인이 없으면 최악이다** — 주문은 나가는데 결과를 모른다.
대장이 `sent` 로 영원히 남고, 포지션이 안 잡히고, 다음 판단이 보유를 모른다.

## 대조가 양방향이어야 한다

- 대장엔 `sent` 인데 체결 내역에 없다 → 아직 미체결이거나 **주문이 실패했다**
- 체결 내역엔 있는데 대장에 없다 → **우리가 내지 않은 주문이다.** 사람이 HTS 로 냈거나
  중복 주문이거나 다른 프로세스다. 조용히 넘기면 계좌 상태를 영영 못 맞춘다

## 필드명을 추측하지 않는다

모의계좌에 주문이 없어 응답 필드를 실물로 확인하지 못했다(2026-09-01).
**후보 이름을 여럿 두고 없으면 실패로 남긴다** — 잘못 읽은 값으로 대장을 채우면
"체결됐다고 믿는데 아닌" 상태가 된다. 어댑터를 붙이는 날 실제 응답으로 좁힌다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("gate.fills")

EXEC_TR = "ka10076"  # 체결요청
OPEN_TR = "ka10075"  # 미체결요청
ACNT_PATH = "/api/dostk/acnt"

# 실물 응답을 못 봤다. 후보를 두되 **없으면 None 이지 0 이 아니다.**
_ORD_NO = ("ord_no", "odr_no", "orgn_ord_no")
_CODE = ("stk_cd", "stk_code")
_QTY = ("cntr_qty", "cnfm_qty", "trde_qty")
_PRICE = ("cntr_uv", "cntr_pric", "trde_uv")


def _pick(d: dict, names: tuple[str, ...]) -> str | None:
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return None


def _num(raw: str | None) -> int | None:
    """부호 접두사를 뗀다 — 키움의 `-257000` 은 하락 표시이지 음수가 아니다."""
    if raw is None:
        return None
    t = raw.lstrip("+-").replace(",", "")
    return int(t) if t.isdigit() else None


@dataclass(frozen=True)
class Execution:
    order_no: str
    code: str
    qty: int
    price: int


def fetch(client, *, tr: str = EXEC_TR) -> tuple[list[Execution], list[str]]:
    """체결 내역. `(읽은 것, 못 읽은 사유)` 를 함께 돌려준다.

    **못 읽은 행을 버리지 않는다** — 필드명이 어긋나면 그 사실이 드러나야 한다.
    """
    body = (
        {"qry_tp": "0", "sell_tp": "0", "stex_tp": "0"}
        if tr == EXEC_TR
        else {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}
    )
    j = client.post(tr, ACNT_PATH, body)
    key = "cntr" if tr == EXEC_TR else "oso"
    out: list[Execution] = []
    problems: list[str] = []
    for row in j.get(key) or []:
        no, code = _pick(row, _ORD_NO), _pick(row, _CODE)
        qty, price = _num(_pick(row, _QTY)), _num(_pick(row, _PRICE))
        if not (no and code and qty is not None and price is not None):
            problems.append(
                f"체결 행을 읽을 수 없다 — 주문번호={no!r} 종목={code!r} "
                f"수량={qty!r} 단가={price!r} · 실제 키 {sorted(row)[:8]}"
            )
            continue
        out.append(Execution(no, code, qty, price))
    return out, problems


def reconcile(conn, executions: list[Execution]) -> dict:
    """대장과 체결 내역을 **양방향**으로 맞춘다. `sent` 만 갱신한다.

    한쪽만 보면 "우리가 내지 않은 주문"을 영영 못 본다.
    """
    ledger = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT broker_ref, intent_id, code FROM order_intents "
            "WHERE status = 'sent' AND broker_ref IS NOT NULL"
        )
    }
    matched, unknown = [], []
    for e in executions:
        hit = ledger.get(e.order_no)
        if hit is None:
            unknown.append(e)
            continue
        intent_id, code = hit
        if code != e.code:
            unknown.append(e)  # 주문번호는 맞는데 종목이 다르다 — 대조가 깨진 것이다
            continue
        conn.execute(
            "UPDATE order_intents SET status='filled', qty=?, fill_price=? WHERE intent_id=?",
            (e.qty, e.price, intent_id),
        )
        matched.append(e)

    seen = {e.order_no for e in executions}
    pending = [
        r[0]
        for r in conn.execute(
            "SELECT broker_ref FROM order_intents WHERE status='sent' AND broker_ref IS NOT NULL"
        )
        if r[0] not in seen
    ]
    # broker_ref 가 없는 sent 는 어댑터가 주문번호를 못 받은 것이다 — 대조 자체가 불가능하다
    unref = conn.execute(
        "SELECT COUNT(*) FROM order_intents WHERE status='sent' AND broker_ref IS NULL"
    ).fetchone()[0]
    return {
        "matched": matched,
        "unknown": unknown,
        "pending": pending,
        "unreferenced": unref,
    }


__all__ = ["ACNT_PATH", "EXEC_TR", "OPEN_TR", "Execution", "fetch", "reconcile"]
