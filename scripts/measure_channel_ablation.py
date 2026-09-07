"""채널을 빼면 유니버스가 나아지는가.

## 왜 이 순서인가

실측(2026-09-07, 60거래일): **유니버스가 모집단보다 나빴다** — +20일 중앙 −7.75% 대
−4.94%. 후보를 좁히는 행위가 가치를 빼고 있었다. 채널별로는 momentum −12.20%,
flow −6.51%, briefing +1.45% 였다.

**고치기 전에 "빼면 나아지나"를 먼저 잰다.** 안 그러면 고쳐서 나아진 것인지
빼서 나아진 것인지 구분되지 않는다.

    python scripts/measure_channel_ablation.py [--days 60]

## 아는 한계

- 채널·하드필터를 **지금 코드**로 과거에 대고 재구성한다.
- 생존 편향 — 상장폐지 종목은 봉이 없다. 실제는 이보다 나쁠 것이다.
- 측정 구간이 하락장이다(모집단 중앙 −4.94%). **절대값이 아니라 차이를 본다.**
- 정원을 재분배해도 **후보 수가 같아지지는 않는다** — 채널마다 조건을 만족하는
  종목 수가 다르다. 표본 크기를 함께 봐야 한다.
"""

from __future__ import annotations

import argparse
import collections
from datetime import date

from data import store
from decision import config as ccfg
from decision import universe as U
from scripts.measure_channel_fit import _fwd, fwd_header, fwd_row, trading_days

# 정원 총합을 60으로 맞춘다 — 채널을 빼고 정원을 그대로 두면 "빠져서 좋아진 것"과
# "후보가 줄어서 좋아진 것"이 섞인다.
TOTAL = sum(ccfg.CHANNEL_QUOTA.values())


def _quota(channels: tuple[str, ...]) -> dict[str, int]:
    each = TOTAL // len(channels)
    return dict.fromkeys(channels, each)


CONFIGS: list[tuple[str, tuple[str, ...] | None]] = [
    ("지금 (3채널)", None),
    ("momentum 제외", ("briefing", "flow")),
    ("flow 제외", ("briefing", "momentum")),
    ("briefing 제외", ("momentum", "flow")),
    ("briefing 만", ("briefing",)),
    ("flow 만", ("flow",)),
    ("momentum 만", ("momentum",)),
]


def run(conn, days: list[str], channels: tuple[str, ...] | None) -> dict[int, list[float]]:
    out: dict[int, list[float]] = collections.defaultdict(list)
    q = None if channels is None else _quota(channels)
    for d in days:
        for cand in U.build(conn, date.fromisoformat(d), channels=channels, quota=q).candidates:
            for h, r in _fwd(conn, cand.code, d).items():
                out[h].append(r)
    return out


def main() -> int:
    p = argparse.ArgumentParser(prog="measure_channel_ablation")
    p.add_argument("--days", type=int, default=60)
    a = p.parse_args()

    with store.connect() as conn:
        days = trading_days(conn, a.days)
        pool: dict[int, list[float]] = collections.defaultdict(list)
        for d in days:
            for code in U.hard_filter(conn, date.fromisoformat(d)):
                for h, r in _fwd(conn, code, d).items():
                    pool[h].append(r)
        rows = [(name, run(conn, days, ch)) for name, ch in CONFIGS]

    print(f"기간 {days[0]} ~ {days[-1]} ({len(days)}거래일) · 정원 총합 {TOTAL}\n")
    print(fwd_header())
    print(fwd_row("모집단 (기준선)", pool))
    print()
    for name, r in rows:
        print(fwd_row(name, r, base=pool))
    print("\n※ 지금 코드로 과거를 재구성했다 · 생존 편향 있음 · 구간이 하락장이다")
    print("※ **구간마다 표본 수가 다르다** — 얇은 칸의 차이는 결론이 아니다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
