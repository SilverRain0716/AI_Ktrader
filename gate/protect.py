"""기계 안전망 — **판단을 기다리지 않고 나간다.**

## 왜 별도인가

`invalidation`(수급·재료 소멸)은 **판단**이다. ADR 0013 원칙 2 가 그렇게 정했고,
감시기는 표시만 하고 재판단을 띄운다.

여기 둘은 **봉투**다(ADR 0009). AI 의견을 묻지 않는다.

- `stop_price` 도달 — 재난 방지선이다. 포지션 크기가 이 폭에서 나왔으므로
  여기를 넘으면 크기 산정의 전제가 깨진 것이다
- `max_hold_days` 초과 — 스윙의 시간축을 벗어났다

**이것이 없으면 AI 가 매 사이클 빠짐없이 봐주는 것에 전부 걸린다.** 실제로
2026-09-07 에 arm 2 가 손절선의 3/4 까지 온 2 종목을 통째로 빠뜨렸다. 그리고
`stop_price` 를 읽는 코드가 저장하는 쪽 말고는 **한 줄도 없었다.**

## 왜 결정 행을 남기는가

주문은 게이트를 지나야 한다 — 킬 스위치·모드·멱등성이 거기 있다. 게이트는
`decisions` 를 보고 판정하므로 강제 청산도 행으로 남긴다. **`run_kind='protect'`**
로 갈라 두면 abstain 비율·F2·F3 같은 판단 통계가 오염되지 않는다(그 쪽은
`run_kind='live'` 만 센다).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from data import config as dcfg

log = logging.getLogger("gate.protect")

RUN_KIND = "protect"
STOP, EXPIRY = "stop", "expiry"


@dataclass(frozen=True)
class Breach:
    position_id: str
    code: str
    name: str
    arm: int
    kind: str
    reason: str


def _held_days(conn: sqlite3.Connection, code: str, opened_at: str, day: str) -> int:
    """거래일 기준이다. 달력일로 세면 주말이 보유기간에 들어간다."""
    return conn.execute(
        "SELECT COUNT(*) FROM ohlcv WHERE code=? AND date>? AND date<=? AND volume>0",
        (code, opened_at, day),
    ).fetchone()[0]


def scan(conn: sqlite3.Connection, day: date | str) -> list[Breach]:
    """열린 포지션에서 봉투를 벗어난 것을 찾는다. **아무것도 쓰지 않는다.**"""
    day = day.isoformat() if isinstance(day, date) else day
    out: list[Breach] = []
    rows = conn.execute(
        "SELECT position_id, code, name, arm, opened_at, stop_price, max_hold_days "
        "FROM paper_positions WHERE closed_at IS NULL"
    ).fetchall()
    for pid, code, name, arm, opened_at, stop, max_days in rows:
        bar = conn.execute(
            "SELECT low, close FROM ohlcv WHERE code=? AND date<=? AND volume>0 AND halted=0 "
            "ORDER BY date DESC LIMIT 1",
            (code, day),
        ).fetchone()
        if stop is not None and bar is not None:
            low, close = bar
            # **저가로 판정한다.** 종가만 보면 장중에 손절선을 크게 뚫고 되돌아온 날을
            # "안 닿았다"고 읽는다 — 실계좌에서는 이미 체결됐을 자리다.
            if low <= stop:
                out.append(
                    Breach(
                        pid,
                        code,
                        name,
                        arm,
                        STOP,
                        f"손절선 {stop:,} 도달 (저가 {low:,} · 종가 {close:,})",
                    )
                )
                continue
        if max_days is not None:
            held = _held_days(conn, code, opened_at, day)
            if held >= max_days:
                out.append(
                    Breach(pid, code, name, arm, EXPIRY, f"보유 {held}거래일 ≥ 기한 {max_days}일")
                )
    return out


def decision_id(day: str, arm: int) -> str:
    """`-a<N>` 형식을 지킨다 — 게이트가 여기서 arm 을 읽는다."""
    return f"{day}-protect-a{arm}"


def enforce(
    conn: sqlite3.Connection, breaches: list[Breach], *, day: str, now: datetime | None = None
) -> int:
    """강제 청산 결정을 남긴다. **주문은 내지 않는다** — 게이트와 브로커가 낸다.

    이미 같은 날 같은 종목으로 남긴 것이 있으면 다시 만들지 않는다(멱등).
    """
    now = now or datetime.now(dcfg.KST)
    by_arm: dict[int, list[Breach]] = {}
    for b in breaches:
        by_arm.setdefault(b.arm, []).append(b)

    n = 0
    for arm, items in by_arm.items():
        did = decision_id(day, arm)
        payload = {
            "market_view": None,
            "abstain": False,
            "abstain_reason": None,
            "decisions": [
                {
                    "action": "EXIT",
                    "code": b.code,
                    "name": b.name,
                    "weight_pct": None,
                    "rank": None,
                    "entry": None,
                    "stop": None,
                    "target": None,
                    "trail": None,
                    "max_hold_days": None,
                    "confidence": None,
                    "reasons": [b.reason],
                    "invalidation": None,
                    "briefing_refs": [],
                    "sources": [],
                }
                for b in items
            ],
            "portfolio_note": "기계 안전망 — 판단이 아니다 (ADR 0009 봉투)",
            "data_concerns": [],
        }
        cur = conn.execute(
            "INSERT OR IGNORE INTO decisions (decision_id,attempt,pack_id,pack_sha256,arm,"
            "run_kind,cycle,generated_at,valid_until,render_version,status,payload) "
            "VALUES (?,1,?,'-',?,?,'protect',?,?,'-','ok',?)",
            (
                did,
                f"protect-{day}",
                arm,
                RUN_KIND,
                now.isoformat(timespec="seconds"),
                # 봉투는 **그날 안에 집행된다.** 이월하면 안전망이 아니라 밀린 주문이다.
                f"{day}T15:20:00+09:00",
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        n += cur.rowcount if cur.rowcount > 0 else 0
    return n


__all__ = ["EXPIRY", "RUN_KIND", "STOP", "Breach", "decision_id", "enforce", "scan"]
