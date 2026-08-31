"""급변 스캔 — 일봉에서 "오늘 무언가 시작됐다"를 찾는다.

## 저장하지 않는다

거래대금·등락률은 일봉(종가·거래량)에서 계산되므로 **13개월 전 어느 날이든 그대로
재현된다.** 저장하면 오히려 해롭다 — [11.7](../docs/01-context-pack-design.md) 이 적은 그대로
**"파생값은 규칙이 바뀌면 낡는다."** 임계를 조정하는 순간 과거 저장분이 전부 다른 규칙의
산물이 된다.

그래서 테이블을 만들지 않고 순수 함수로 둔다. 리플레이에서 같은 `day` 로 부르면 같은 답이 나온다.

## 두 축을 함께 본다

**거래대금 배수만으로는 대형주를 구조적으로 못 잡는다.** 실측(2026-08-28, SK이노베이션):
시총 19.8조 · 일평균 거래대금 1,099억 · 당일 1,476억 → **배수 1.34배**. 3배가 되려면
3,300억이 필요하다. 분모가 크면 배수는 뜨지 않는다.

같은 날 두 축을 나란히 재면 서로 다른 것을 잡는다(661종목).

| 축 | SK이노베이션 | 상위를 채우는 것 |
|---|---|---|
| 절대 거래대금 | **16위** | 대형주 (상위 10 중 9개가 시총 1조 이상) |
| 배수 | 98위 | 소형주 (쿠콘 14.9배 · 신풍제약 13.8배, 시총 3~5천억) |

## 이것은 채널이 아니다 — 아직

[ADR 0006](../docs/adr/0006-edge-hypothesis.md) 이 이질 채널 추가를 F2 이후로 막았고, 급변 스캔은
**후보를 만드는 일**이므로 명백히 채널이다. 팩에 싣지 않는다. 실험 7 이 답할 때까지 관측만 한다
([ADR 0011](../docs/adr/0011-event-scan.md)).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

log = logging.getLogger("data.events")

# ── 임계 — **전부 임시값이다.** 실험 7 이 정한다 (ADR 0011).
#
# ADR 0006 이 F1 임계 3%p 에 한 것과 같은 방식으로, 근거 없는 값을 근거 있는 것처럼
# 두지 않기 위해 여기 적어 둔다.
VOLUME_MULTIPLE = 3.0  # relative 축: 20일 평균 거래대금 대비 배수
ABSOLUTE_TOP_N = 20  # absolute 축: 당일 거래대금 전체 상위 N 위
# **등락률은 ATR 배수다. 고정 % 가 아니다** — ADR 0009 가 갭 가드에서 정한 것과 같은 원칙이다.
# 고정 5% 로 두면 대형주가 통째로 빠진다: SK이노베이션 +4.38% 는 시총 19.8조에서 큰 움직임인데
# (ATR 7.06% 기준 0.62배) 5% 문턱에 걸리지 않았다. 반대로 원익홀딩스 -5.8% 는 ATR 11.1% 라
# 0.52배에 불과한데 고정 5% 는 통과시켰다. 둘 다 틀린 판정이다.
MIN_CHANGE_ATR = 0.6  # 두 축 공통: |등락률| / ATR (급락도 사건이므로 절댓값)
MIN_VALUE_EOK = 100.0  # 거래대금 하한. 이보다 얇으면 배수가 의미 없다
LOOKBACK = 20  # 평균 산출 구간

REL, ABS = "relative", "absolute"


@dataclass(frozen=True)
class EventHit:
    code: str
    day: str
    change_pct: float
    change_atr: float  # |등락률| / ATR — 종목 변동성으로 정규화한 크기
    value_eok: float
    volume_multiple: float
    value_rank: int
    axes: tuple[str, ...]  # 어느 축으로 걸렸는가 — 축별 성과를 갈라야 한다

    @property
    def direction(self) -> str:
        return "up" if self.change_pct > 0 else "down"


def scan(
    conn: sqlite3.Connection,
    day: date | str,
    *,
    volume_multiple: float = VOLUME_MULTIPLE,
    absolute_top_n: int = ABSOLUTE_TOP_N,
    min_change_atr: float = MIN_CHANGE_ATR,
    min_value_eok: float = MIN_VALUE_EOK,
) -> list[EventHit]:
    """그날의 급변 종목. **일봉만 읽고 아무것도 쓰지 않는다.**

    `day` 이후 데이터는 보지 않는다 — 리플레이에서 미래가 새면 11.9 의 사고가 재현된다.
    """
    day = day.isoformat() if isinstance(day, date) else day

    # 종목별 최근 LOOKBACK+1 거래일. day 를 넘는 행은 애초에 읽지 않는다.
    # ATR 은 지표 테이블에 있다. 없는 종목은 크기를 정규화할 수 없으므로 제외한다 —
    # 고정 % 로 물러서면 대형주·소형주에 같은 잣대를 대는 문제가 되돌아온다.
    atr_pct: dict[str, float] = {}
    for code, payload in conn.execute("SELECT code, payload FROM indicators"):
        v = (json.loads(payload).get("indicators") or {}).get("atr_pct")
        if v:
            atr_pct[code] = float(v)

    rows = conn.execute(
        """
        SELECT code, date, close, volume FROM ohlcv
        WHERE halted = 0 AND date <= ? AND date >= date(?, ?)
        ORDER BY code, date
        """,
        (day, day, f"-{LOOKBACK * 3} days"),  # 휴장일 여유를 두고 넉넉히 당긴다
    ).fetchall()

    series: dict[str, list[tuple[str, float, float]]] = {}
    for code, d, close, volume in rows:
        if close is None or volume is None:
            continue
        series.setdefault(code, []).append((d, float(close), float(close) * volume / 1e8))

    # ── 1차: 종목별 지표 계산
    stats = []
    for code, s in series.items():
        if len(s) < LOOKBACK + 1 or s[-1][0] != day:
            continue  # 당일 봉이 없으면 거래정지·미상장이다
        _, close, value = s[-1]
        prev_close = s[-2][1]
        adv = sum(x[2] for x in s[-(LOOKBACK + 1) : -1]) / LOOKBACK
        if adv <= 0 or prev_close <= 0:
            continue
        atr = atr_pct.get(code)
        if not atr:
            continue
        change = (close - prev_close) / prev_close * 100
        stats.append((code, change, abs(change) / atr, value, value / adv))

    # ── 2차: 절대 순위는 그날 전체를 봐야 정해진다
    ranked = sorted(stats, key=lambda x: -x[3])
    rank_of = {code: i + 1 for i, (code, *_) in enumerate(ranked)}

    hits = []
    for code, change_pct, change_atr, value, multiple in stats:
        if change_atr < min_change_atr or value < min_value_eok:
            continue
        axes = []
        if multiple >= volume_multiple:
            axes.append(REL)
        if rank_of[code] <= absolute_top_n:
            axes.append(ABS)
        if not axes:
            continue
        hits.append(
            EventHit(
                code=code,
                day=day,
                change_pct=round(change_pct, 2),
                change_atr=round(change_atr, 2),
                value_eok=round(value, 1),
                volume_multiple=round(multiple, 2),
                value_rank=rank_of[code],
                axes=tuple(axes),
            )
        )
    return sorted(hits, key=lambda h: -h.value_eok)


def scan_range(conn: sqlite3.Connection, start: str, end: str, **kw) -> dict[str, list[EventHit]]:
    """구간 스캔. 실험 7 이 쓴다 — 저장하지 않고 매번 계산한다."""
    days = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM ohlcv WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
    ]
    return {d: scan(conn, d, **kw) for d in days}


__all__ = ["ABS", "REL", "EventHit", "scan", "scan_range"]
