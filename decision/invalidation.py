"""무효화 감시 — 진입할 때 적은 조건이 깨졌는지 판정한다.

## 왜 이것이 필요한가

[ADR 0013](../docs/adr/0013-trading-doctrine.md) 원칙 2 는 *"익절·손절은 % 로 계산하지 않는다.
수급과 이슈, 재료의 소멸로 판단한다"* 이다. 그 조건을 적는 자리가 `invalidation` 이고,
**여기가 그것을 실제로 보는 코드다.**

이게 없는 동안 청산은 `stop`(가격)과 `max_hold_days`(시간)로만 일어났다 — **둘 다 원칙 2 가
쓰지 말라는 방식**이다. 조건은 적히는데 아무도 안 봤다.

## 역할 분담

| | 역할 | 기준 |
|---|---|---|
| `invalidation` | **주 청산 판단** | 수급 이탈 · 관심 소멸 · 악재 공시 |
| `stop` | 재난 방지선 + **포지션 크기 산출 근거** | 2 ATR |

`stop` 은 평소에 닿지 않아야 한다. **닿는 빈도가 곧 `invalidation` 설계의 품질 지표다** —
자주 닿는다면 재료 소멸을 못 잡고 있다는 뜻이다.

## 판정하지 않는 것을 판정했다고 하지 않는다

조건을 평가할 데이터가 없으면 `UNKNOWN` 이다. **`False`(안 깨졌다)로 접지 않는다** —
이 저장소가 반복해 당한 실패 방식이 바로 그것이다(빈 테이블 조회가 조용히 통과한 악재공시 필터).
`UNKNOWN` 은 포지션을 건드리지 않지만 사유가 남는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

log = logging.getLogger("decision.invalidation")

HIT, SAFE, UNKNOWN = "hit", "safe", "unknown"

# 수급 연속 순매도를 셀 때 거슬러 올라가는 최대 거래일. 이보다 길게 세지 않는다.
FLOW_LOOKBACK = 20
# 거래대금 배수의 분모 구간 (ADR 0011 과 같은 20일)
VALUE_LOOKBACK = 20


@dataclass(frozen=True)
class Verdict:
    position_id: str
    code: str
    state: str  # HIT / SAFE / UNKNOWN
    reason: str
    observed: float | str | None = None

    @property
    def hit(self) -> bool:
        return self.state == HIT


def _bars(conn: sqlite3.Connection, code: str, on: str, n: int) -> list[tuple[str, float, float]]:
    """(날짜, 종가, 거래대금 억원) 최신순. `on` 이후는 보지 않는다."""
    rows = conn.execute(
        "SELECT date, close, volume FROM ohlcv "
        "WHERE code = ? AND date <= ? AND halted = 0 AND volume > 0 "
        "ORDER BY date DESC LIMIT ?",
        (code, on, n),
    ).fetchall()
    return [(d, float(c), float(c) * v / 1e8) for d, c, v in rows if c and v]


def _flow_sell_streak(conn: sqlite3.Connection, code: str, on: str) -> int | None:
    """외국인·기관이 **둘 다** 순매도인 날이 며칠 연속인가. 데이터가 없으면 None.

    한쪽만 파는 것은 흔하다. 진입 근거가 '외인 또는 기관 매집'이었으므로 그 반대는
    **양쪽이 함께 도는 것**으로 본다 — 한쪽 기준으로 두면 거의 매일 참이 된다.
    """
    rows = conn.execute(
        "SELECT date, foreign_net_qty, inst_net_qty FROM flows "
        "WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?",
        (code, on, FLOW_LOOKBACK),
    ).fetchall()
    if not rows:
        return None
    streak = 0
    for _d, f, i in rows:
        if f is None or i is None:
            break
        if f < 0 and i < 0:
            streak += 1
        else:
            break
    return streak


def _value_ratio(conn: sqlite3.Connection, code: str, on: str) -> float | None:
    """당일 거래대금 / 직전 20일 평균. 봉이 모자라면 None."""
    bars = _bars(conn, code, on, VALUE_LOOKBACK + 1)
    if len(bars) < VALUE_LOOKBACK + 1:
        return None
    today = bars[0][2]
    prior = [b[2] for b in bars[1:]]
    adv = sum(prior) / len(prior)
    return None if adv <= 0 else today / adv


def _ma(conn: sqlite3.Connection, code: str, on: str, period: int) -> tuple[float, float] | None:
    bars = _bars(conn, code, on, period)
    if len(bars) < period:
        return None
    return bars[0][1], sum(b[1] for b in bars) / period


def evaluate(
    conn: sqlite3.Connection, inv: dict, code: str, on: date | str, *, position_id: str = ""
) -> Verdict:
    """조건 하나를 판정한다. **평가할 수 없으면 UNKNOWN 이지 SAFE 가 아니다.**"""
    on = on.isoformat() if isinstance(on, date) else on
    t, val = inv.get("type"), inv.get("value")
    v = lambda s, r, o=None: Verdict(position_id, code, s, r, o)  # noqa: E731

    deadline = inv.get("deadline")
    if deadline and on > deadline:
        return v(SAFE, f"감시 기한({deadline})이 지났다 — max_hold_days 가 기한을 맡는다")

    if t == "unstructured":
        return v(UNKNOWN, "unstructured 는 기계가 감시할 수 없다 (ADR 0007)")

    if t == "flow_reversal":
        streak = _flow_sell_streak(conn, code, on)
        if streak is None:
            return v(UNKNOWN, "수급 데이터가 없다")
        need = int(val)
        return (
            v(HIT, f"외국인·기관 동반 순매도 {streak}일 연속 (기준 {need}일)", streak)
            if streak >= need
            else v(SAFE, f"동반 순매도 {streak}일 (기준 {need}일)", streak)
        )

    if t == "volume_dryup":
        ratio = _value_ratio(conn, code, on)
        if ratio is None:
            return v(UNKNOWN, "거래대금 20일 평균을 낼 봉이 모자란다")
        return (
            v(HIT, f"거래대금 20일 평균의 {ratio:.2f}배 (기준 {val}배 미만)", round(ratio, 3))
            if ratio < float(val)
            else v(SAFE, f"거래대금 {ratio:.2f}배 (기준 {val}배)", round(ratio, 3))
        )

    if t == "price_below":
        bars = _bars(conn, code, on, 1)
        if not bars:
            return v(UNKNOWN, "당일 봉이 없다 (거래정지 가능)")
        close = bars[0][1]
        return (
            v(HIT, f"종가 {close:,.0f} < 기준 {float(val):,.0f}", close)
            if close < float(val)
            else v(SAFE, f"종가 {close:,.0f} (기준 {float(val):,.0f})", close)
        )

    if t == "close_below_ma":
        got = _ma(conn, code, on, int(val))
        if got is None:
            return v(UNKNOWN, f"{val}일 이동평균을 낼 봉이 모자란다")
        close, ma = got
        return (
            v(HIT, f"종가 {close:,.0f} < MA{int(val)} {ma:,.0f}", close)
            if close < ma
            else v(SAFE, f"종가 {close:,.0f} ≥ MA{int(val)} {ma:,.0f}", close)
        )

    if t == "disclosure_category":
        row = conn.execute(
            "SELECT report_nm FROM disclosures WHERE code = ? AND category = ? "
            "AND rcept_dt <= ? ORDER BY rcept_dt DESC LIMIT 1",
            (code, str(val), on.replace("-", "")),
        ).fetchone()
        return (
            v(HIT, f"'{val}' 공시 발생: {row[0][:60]}", row[0])
            if row
            else v(SAFE, f"'{val}' 공시 없음")
        )

    if t == "stance_reversal":
        # 브리핑 관점 반전은 briefing_views 에 판정이 쌓여야 판단할 수 있다.
        # 지금은 그 판정을 만드는 코드가 없다 — 없는 것을 있는 척하지 않는다.
        return v(UNKNOWN, "브리핑 관점 반전 판정기가 아직 없다")

    return v(UNKNOWN, f"알 수 없는 invalidation.type={t!r}")


def scan(conn: sqlite3.Connection, on: date | str) -> list[Verdict]:
    """열려 있는 페이퍼 포지션 전부를 판정한다. **아무것도 쓰지 않는다.**"""
    on = on.isoformat() if isinstance(on, date) else on
    out = []
    for pid, code, raw in conn.execute(
        "SELECT position_id, code, invalidation FROM paper_positions "
        "WHERE closed_at IS NULL AND invalidation IS NOT NULL"
    ):
        try:
            inv = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            out.append(Verdict(pid, code, UNKNOWN, "invalidation JSON 을 읽을 수 없다"))
            continue
        out.append(evaluate(conn, inv, code, on, position_id=pid))
    return out


def mark_hits(conn: sqlite3.Connection, verdicts: list[Verdict]) -> int:
    """깨진 것에 `invalidation_hit = 1` 을 찍는다. **청산하지 않는다.**

    청산은 실행 계층의 일이고 그것은 아직 0줄이다. 여기서 포지션을 닫으면
    킬 스위치도 멱등성도 없는 자리에서 상태를 바꾸는 것이 된다 — 표시만 남기고
    재판단(event 사이클)을 띄우는 것이 [ADR 0009](../docs/adr/0009-entry-timing.md) 의 순서다.
    """
    hits = [x for x in verdicts if x.hit]
    conn.executemany(
        "UPDATE paper_positions SET invalidation_hit = 1 WHERE position_id = ?",
        [(x.position_id,) for x in hits],
    )
    return len(hits)


__all__ = ["HIT", "SAFE", "UNKNOWN", "Verdict", "evaluate", "mark_hits", "scan"]
