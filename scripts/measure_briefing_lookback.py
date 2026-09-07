"""브리핑을 며칠까지 거슬러 보는 것이 좋은가.

## 왜 재고 나서 바꾸는가

실측(2026-09-07, 60거래일): briefing 채널만 쓰면 모집단보다 **+6.40%p** 였다.
그런데 **표본이 147건뿐**이다 — `BRIEFING_LOOKBACK_DAYS = 3` 이라 하루 2~3종목만 올라온다.

표본을 키우려면 기간을 늘려야 하는데, **늘리면 오래된 관점이 섞인다.** 브리핑은
그날의 판단이고 며칠 지나면 재료가 소진됐을 수 있다 — 늘려서 표본이 커져도
수익률이 무너지면 그건 개선이 아니다. **바꾸기 전에 그 곡선을 본다.**

    python -m scripts.measure_briefing_lookback

## 아는 한계

- 지금 코드로 과거를 재구성한다 · 생존 편향 · 구간이 하락장이다.
- 브리핑 자체가 며칠치만 쌓여 있어서 **긴 기간일수록 실제로 늘어나는 폭이 작다.**
"""

from __future__ import annotations

import argparse
import collections
from datetime import date

from data import store
from decision import config as ccfg
from decision import universe as U
from scripts.measure_channel_fit import _fwd, fwd_header, fwd_row, trading_days

LOOKBACKS = (1, 3, 5, 10, 20, 40)


def run(conn, days: list[str], lookback: int) -> dict[int, list[float]]:
    out: dict[int, list[float]] = collections.defaultdict(list)
    old = ccfg.BRIEFING_LOOKBACK_DAYS
    ccfg.BRIEFING_LOOKBACK_DAYS = lookback
    try:
        for d in days:
            picks = U.build(
                conn,
                date.fromisoformat(d),
                channels=("briefing",),
                quota={"briefing": ccfg.UNIVERSE_MAX},
            ).candidates
            for cand in picks:
                for h, r in _fwd(conn, cand.code, d).items():
                    out[h].append(r)
    finally:
        ccfg.BRIEFING_LOOKBACK_DAYS = old
    return out


def main() -> int:
    p = argparse.ArgumentParser(prog="measure_briefing_lookback")
    p.add_argument("--days", type=int, default=60)
    a = p.parse_args()

    with store.connect() as conn:
        days = trading_days(conn, a.days)
        pool: dict[int, list[float]] = collections.defaultdict(list)
        for d in days:
            for code in U.hard_filter(conn, date.fromisoformat(d)):
                for h, r in _fwd(conn, code, d).items():
                    pool[h].append(r)
        rows = [(lb, run(conn, days, lb)) for lb in LOOKBACKS]

    print(f"기간 {days[0]} ~ {days[-1]} ({len(days)}거래일)\n")
    print(fwd_header())
    print(fwd_row("모집단", pool))
    print()
    for lb, r in rows:
        print(fwd_row(f"lookback {lb}", r, base=pool))
    print("\n※ 지금 코드로 과거 재구성 · 생존 편향 · 하락장 구간")
    print("※ **+20일 표본이 안 늘면 기간을 늘려도 그 칸은 그대로다** — 브리핑이")
    print("   최근 것뿐이라 20거래일 이후 데이터가 있는 건 극소수다 (실측 14건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
