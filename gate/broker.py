"""주문 브로커 — **접수와 체결을 나눈다.** 지금 있는 것은 시뮬레이터뿐이다.

## 왜 두 단계인가

주문을 낸 순간에는 체결됐는지 알 수 없다. 실제 증권사도 그렇고, 시뮬레이터도 그래야
같은 인터페이스가 된다. **그날 일봉이 들어온 뒤에야 체결을 판정할 수 있다.**

    place()   게이트 통과분 → 수량 계산 → status='sent'
    settle()  그날 일봉으로 체결 판정 → 'filled' | 'expired' | 'gapped'

한 단계로 합치면 시뮬레이터에서는 되지만 실제 어댑터로 바꾸는 순간 구조가 깨진다 —
[CLAUDE.md](../CLAUDE.md) 가 경고한 *"주문 어댑터만 교체"* 가 불가능해진다.

## 봉투는 여기서 강제된다 (ADR 0009)

- **갭 가드** — 시가가 전일 종가 대비 ATR 배수 밴드를 벗어나면 집행하지 않는다.
  고정 % 가 아니다: ATR 4% 종목과 13% 종목에 같은 잣대를 대면 안 된다
- **미체결은 폐기, 이월 없음** — 그날 안 되면 끝이다
- 이것은 **전 arm 공통**이다. arm 마다 다르게 두면 F2 가 "AI 의 선택"이 아니라
  집행 규칙 차이를 잰다

## 실제 주문은 여기 없다

`SimBroker` 는 아무 데도 요청을 보내지 않는다. 실제 어댑터는 `execution/` 에 들어가고,
그것은 저장소가 private 이 된 뒤의 일이다(하드 규칙 1·5).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from decision import config as ccfg

log = logging.getLogger("gate.broker")

SENT, FILLED, EXPIRED, GAPPED = "sent", "filled", "expired", "gapped"
# 새 사이클의 판단이 나와 옛 주문이 무효가 된 상태. **이월 금지**(ADR 0009 결정 3).
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Fill:
    intent_id: str
    code: str
    status: str
    qty: int = 0
    price: int = 0
    reason: str = ""


class Broker(Protocol):
    """실제 어댑터가 따라야 할 모양. **교체 지점은 여기 하나여야 한다.**"""

    name: str

    def place(self, conn: sqlite3.Connection, decision_id: str, *, now: datetime) -> list[Fill]: ...

    def settle(self, conn: sqlite3.Connection, day: str) -> list[Fill]: ...


def _atr_pct(conn: sqlite3.Connection, code: str, on: str) -> float | None:
    row = conn.execute(
        "SELECT payload FROM indicators WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, on),
    ).fetchone()
    if not row:
        return None
    return (json.loads(row[0]).get("indicators") or {}).get("atr_pct")


def _bar(conn: sqlite3.Connection, code: str, day: str) -> tuple[int, int, int, int] | None:
    row = conn.execute(
        "SELECT open, high, low, close FROM ohlcv WHERE code=? AND date=? AND volume>0 AND halted=0",
        (code, day),
    ).fetchone()
    return tuple(int(x) for x in row) if row and all(row) else None


def _prev_close(conn: sqlite3.Connection, code: str, day: str) -> int | None:
    row = conn.execute(
        "SELECT close FROM ohlcv WHERE code=? AND date<? AND volume>0 ORDER BY date DESC LIMIT 1",
        (code, day),
    ).fetchone()
    return int(row[0]) if row and row[0] else None


def size_for(equity_krw: int, weight_pct: float, price: int) -> int:
    """정수 주수. **못 사면 0이다** — 소수점 주문은 없다.

    실측(2026-09-01, 시드 2천만): 삼성전기 1주가 145.8만원이라 목표 비중 7% 로는 0주다.
    이것은 버그가 아니라 소액 계좌의 현실이고, 페이퍼에서도 같게 재야 실계좌와 맞는다.
    """
    if price <= 0 or weight_pct <= 0:
        return 0
    return int(equity_krw * weight_pct / 100 // price)


class SimBroker:
    """시뮬레이터. **어디에도 요청을 보내지 않는다.**"""

    name = "sim"

    def place(self, conn: sqlite3.Connection, decision_id: str, *, now: datetime) -> list[Fill]:
        """게이트가 `allowed` 로 남긴 의도에 수량을 채우고 접수 상태로 만든다."""
        equity = ccfg.account_seed()["total_equity_krw"]
        day = now.date().isoformat()
        out: list[Fill] = []
        rows = conn.execute(
            "SELECT intent_id, code, action, limit_price FROM order_intents "
            "WHERE decision_id=? AND status='allowed'",
            (decision_id,),
        ).fetchall()
        pay = conn.execute(
            "SELECT payload FROM decisions WHERE decision_id=? ORDER BY attempt DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        weights = {
            d["code"]: d.get("weight_pct") or 0
            for d in (json.loads(pay[0]).get("decisions") or [] if pay and pay[0] else [])
        }
        for intent_id, code, _action, limit_price in rows:
            ref = limit_price or (_prev_close(conn, code, day) or 0)
            qty = size_for(equity, weights.get(code, 0), ref)
            if qty == 0:
                f = Fill(
                    intent_id, code, EXPIRED, 0, 0, f"수량 0 — 1주({ref:,}원)가 목표 비중을 넘는다"
                )
                conn.execute(
                    "UPDATE order_intents SET status=?, reason=?, qty=0 WHERE intent_id=?",
                    (EXPIRED, f.reason, intent_id),
                )
            else:
                f = Fill(intent_id, code, SENT, qty, ref)
                conn.execute(
                    "UPDATE order_intents SET status=?, qty=?, ref_price=? WHERE intent_id=?",
                    (SENT, qty, ref, intent_id),
                )
            out.append(f)
        return out

    def settle(self, conn: sqlite3.Connection, day: str) -> list[Fill]:
        """접수분을 그날 일봉으로 판정한다. **미체결은 폐기이고 이월하지 않는다.**

        `day` 는 **주문 접수일 이후**여야 한다. 이전 봉으로 체결시키면 결정이 이미 본
        데이터가 그대로 체결가가 된다 — 접수일보다 앞선 주문은 건너뛴다.
        """
        up = ccfg._require("AIK_MAX_ENTRY_GAP_UP_ATR")
        down = ccfg._require("AIK_MAX_ENTRY_GAP_DOWN_ATR")
        out: list[Fill] = []
        # `date()` 를 쓰면 안 된다 — SQLite 가 오프셋을 UTC 로 환산해 **KST 새벽이 전날이 된다.**
        # 실측: '2026-09-01T04:56:45+09:00' → date() = '2026-08-31'. 저장된 문자열이 이미
        # KST 이므로 앞 10자를 그대로 자른다.
        #
        # **주문일보다 이전 봉으로 체결시키지 않는다.** 결정이 이미 본 봉으로 체결하면
        # 그 판단의 근거가 곧 체결가가 된다 — 이 저장소가 반복해 당한 미래/과거 누수다.
        # 실제로 그랬다(2026-09-01): 09-01 장을 향한 주문이 08-31 봉으로 체결됐다.
        for intent_id, code, _action, qty, limit_price in conn.execute(
            "SELECT intent_id, code, action, qty, limit_price FROM order_intents "
            "WHERE status='sent' AND substr(created_at, 1, 10) <= ?",
            (day,),
        ).fetchall():
            bar = _bar(conn, code, day)
            if bar is None:
                out.append(
                    self._close(
                        conn, intent_id, code, EXPIRED, reason="그날 봉이 없다 (거래정지·휴장)"
                    )
                )
                continue
            o, _h, low, _c = bar
            prev, atr = _prev_close(conn, code, day), _atr_pct(conn, code, day)
            if prev and atr:
                gap_atr = (o - prev) / prev * 100 / atr
                if gap_atr > up or gap_atr < -down:
                    out.append(
                        self._close(
                            conn,
                            intent_id,
                            code,
                            GAPPED,
                            reason=f"개장 갭 {gap_atr:+.2f} ATR (허용 -{down}~+{up}) — 밤새 전제가 깨졌다",
                        )
                    )
                    continue
            if limit_price:
                if low > limit_price:
                    out.append(
                        self._close(
                            conn,
                            intent_id,
                            code,
                            EXPIRED,
                            reason=f"지정가 {limit_price:,} 미도달 (저가 {low:,})",
                        )
                    )
                    continue
                price = limit_price
            else:
                price = o  # MARKET 은 시가 체결로 본다
            conn.execute(
                # **`limit_price` 를 덮지 않는다** — 지시한 지정가와 체결가는 다른 것이다.
                "UPDATE order_intents SET status=?, fill_price=? WHERE intent_id=?",
                (FILLED, price, intent_id),
            )
            out.append(Fill(intent_id, code, FILLED, qty, price))
        return out

    @staticmethod
    def _close(conn, intent_id: str, code: str, status: str, *, reason: str) -> Fill:
        conn.execute(
            "UPDATE order_intents SET status=?, reason=? WHERE intent_id=?",
            (status, reason, intent_id),
        )
        return Fill(intent_id, code, status, reason=reason)


def supersede(conn: sqlite3.Connection, decision_id: str) -> list[Fill]:
    """이 결정보다 **오래된** 사이클의 미체결을 폐기한다 (ADR 0009 결정 3).

    > 미체결은 15:20 폐기, 이월 금지. 새 사이클의 결정을 집행하기 전에
    > 같은 종목의 직전 사이클 잔여 주문을 먼저 취소한다.

    **`abstain` 도 판단이다.** "지금 상황에서는 사지 않는다"가 새 판단이라면,
    이전 사이클에서 나온 주문은 그 상황에서 나온 것이 아니므로 무효다.
    실제로 그 구멍이 있었다(2026-09-01): v3 가 접수한 2건이 남아 있는데 v4 가
    양쪽 arm 모두 abstain 했고, 그대로 두면 **최신 판단과 어긋난 주문이 체결된다.**

    같은 결정에서 나온 주문은 건드리지 않는다 — 재시도가 자기 주문을 지우면 안 된다.
    """
    row = conn.execute(
        "SELECT generated_at FROM decisions WHERE decision_id=? ORDER BY attempt DESC LIMIT 1",
        (decision_id,),
    ).fetchone()
    if row is None:
        return []
    stale = conn.execute(
        "SELECT i.intent_id, i.code FROM order_intents i "
        "JOIN (SELECT decision_id, MAX(generated_at) g FROM decisions GROUP BY decision_id) d "
        "  ON d.decision_id = i.decision_id "
        "WHERE i.status = ? AND i.decision_id <> ? AND d.g < ?",
        (SENT, decision_id, row[0]),
    ).fetchall()
    out = []
    for intent_id, code in stale:
        conn.execute(
            "UPDATE order_intents SET status=?, reason=? WHERE intent_id=?",
            (SUPERSEDED, f"새 판단({decision_id})이 나와 무효 — 이월 금지 (ADR 0009)", intent_id),
        )
        out.append(Fill(intent_id, code, SUPERSEDED, reason="새 판단으로 무효"))
    return out


def apply_fills(
    conn: sqlite3.Connection, fills: list[Fill], *, day: date | str, arm: int = 1
) -> int:
    """체결된 것만 `paper_positions` 에 반영한다. **주문 대장이 정본이고 여기는 파생이다.**

    `arm` 마다 독립된 가상 계좌다 — 섞으면 3-arm 대응비교가 무너진다.
    """
    from decision import positions as P

    day = day.isoformat() if isinstance(day, date) else day
    n = 0
    for f in fills:
        if f.status != FILLED or f.qty <= 0:
            continue
        name = conn.execute("SELECT name FROM listing WHERE code=? LIMIT 1", (f.code,)).fetchone()
        P.open_position(
            conn,
            position_id=f"{day}-a{arm}-{f.code}",
            arm=arm,
            code=f.code,
            name=name[0] if name else f.code,
            qty=f.qty,
            avg_price=f.price,
            opened_at=day,
        )
        n += 1
    return n


__all__ = [
    "EXPIRED",
    "FILLED",
    "GAPPED",
    "SENT",
    "Broker",
    "Fill",
    "SimBroker",
    "apply_fills",
    "size_for",
]
