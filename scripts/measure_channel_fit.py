"""유니버스 채널이 v8 규칙과 **같은 방향을 보는가.**

## 왜 재는가

세 채널(briefing·momentum·flow)은 전부 **이미 형성된 상태**를 본다 — 이미 오른 종목,
이미 돈이 들어온 종목. CLAUDE.md 가 이미 그렇게 적고 있다.

그런데 `prompts/decision_v8.md` 는 실측을 근거로 *"거래량 폭증과 함께 급등한 자리는
사지 마라"* 고 말한다(+20일 −1.38% vs 기준선 +0.22%).

**후보를 뽑는 기준과 사지 말라는 기준이 반대 방향이면 그 뒤는 다 의미가 없다.**
AI 는 매 사이클 "살 게 없다"고 말하게 되고, 실제로 그러고 있다.

    python scripts/measure_channel_fit.py [--days 20]

## 아는 한계

- 하드 필터·채널은 **지금 코드**로 과거를 재구성한다. 당시와 다를 수 있다.
- 생존 편향 — 상장폐지 종목은 봉이 없다.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from datetime import date

from data import store
from decision import config as ccfg
from decision import universe as U

# v8 표와 같은 경계를 쓴다. 여기서 어긋나면 재는 것이 프롬프트가 말하는 것과 달라진다.
GOOD = "완만"
SPIKE = "폭증급등"
DIST = "물량소화"
OTHER = "그외"
KEYS = (GOOD, SPIKE, DIST, OTHER)


def bucket(vr: float | None, chg: float | None) -> str:
    if vr is None or chg is None:
        return OTHER
    if vr >= 3 and chg >= 5:
        return SPIKE
    if vr >= 3 and -2 < chg < 2:
        return DIST
    if 1.0 <= vr < 1.8 and 1 <= chg < 4:
        return GOOD
    return OTHER


def _ind(conn: sqlite3.Connection, code: str, on: str) -> tuple[float | None, float | None]:
    row = conn.execute(
        "SELECT payload FROM indicators WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
        (code, on),
    ).fetchone()
    if not row:
        return None, None
    i = (json.loads(row[0]).get("indicators") or {}) or {}
    return i.get("volume_ratio"), i.get("change_pct")


def trading_days(conn: sqlite3.Connection, n: int, *, min_codes: int = 100) -> list[str]:
    """**지표가 있는 날만 센다.** 일봉 날짜로 잡으면 지표가 없는 과거일에는 하드 필터가
    거의 비고, 그 빈 날들이 표본에 섞여 "15일을 쟀다"고 착각하게 된다 —
    실제로 지표는 며칠치뿐이다(2026-09-07 기준 5일).
    """
    rows = conn.execute(
        "SELECT date, COUNT(*) c FROM indicators GROUP BY date HAVING c >= ? "
        "ORDER BY date DESC LIMIT ?",
        (min_codes, n),
    ).fetchall()
    return [r[0] for r in rows][::-1]


HORIZONS = (5, 10, 20)


def fwd_row(label: str, r: dict[int, list[float]], *, base: dict | None = None) -> str:
    """**구간마다 표본 수를 함께 찍는다.**

    한 번 크게 당했다(2026-09-08): 표본 수를 `+5일` 기준으로만 찍었더니 briefing 의
    `+20일` 이 **14건**(종목 9개)인 것을 못 봤다. lookback 을 1→40 으로 바꿔도 +20일이
    1.45% 로 고정돼 있었는데 그 신호를 놓쳤다 — 표본이 안 늘고 있었던 것이다.
    구간마다 필요한 봉 수가 달라서 **하나의 n 으로는 표가 만들어지지 않는다.**
    """
    import statistics as st

    cells = []
    for h in HORIZONS:
        v = r.get(h) or []
        if not v:
            cells.append(f"{'-':>22}")
            continue
        m = st.median(v)
        diff = f"{m - st.median(base[h]):>+8.1f}p" if base and base.get(h) else f"{'':>9}"
        cells.append(f"{len(v):>7,}{m:>7.2f}%{diff}")
    return f"{label:<22}" + "".join(cells)


def fwd_header() -> str:
    return f"{'':<22}" + "".join(f"{'+' + str(h) + '일  표본·중앙·차이':>22}" for h in HORIZONS)


def _fwd(conn: sqlite3.Connection, code: str, on: str) -> dict[int, float]:
    """그날 종가 이후 N거래일 수익률. 봉이 모자라면 그 구간은 없다."""
    rows = conn.execute(
        "SELECT close FROM ohlcv WHERE code=? AND date>=? AND volume>0 ORDER BY date LIMIT ?",
        (code, on, max(HORIZONS) + 1),
    ).fetchall()
    if not rows:
        return {}
    base = rows[0][0]
    return {h: (rows[h][0] / base - 1) * 100 for h in HORIZONS if len(rows) > h and base}


def measure(conn: sqlite3.Connection, days: list[str]):
    pool_c: collections.Counter = collections.Counter()
    pick_c: collections.Counter = collections.Counter()
    by_channel: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    # **버킷 구성보다 이쪽이 직접적이다** — 유니버스에 오른 것이 모집단보다 나은가.
    pool_r: dict[int, list[float]] = collections.defaultdict(list)
    pick_r: dict[int, list[float]] = collections.defaultdict(list)
    ch_r: dict[str, dict[int, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for d in days:
        as_of = date.fromisoformat(d)
        for code in U.hard_filter(conn, as_of):
            pool_c[bucket(*_ind(conn, code, d))] += 1
            for h, r in _fwd(conn, code, d).items():
                pool_r[h].append(r)
        for cand in U.build(conn, as_of).candidates:
            b = bucket(*_ind(conn, cand.code, d))
            pick_c[b] += 1
            fwd = _fwd(conn, cand.code, d)
            for h, r in fwd.items():
                pick_r[h].append(r)
            for ch in cand.channels:
                by_channel[ch][b] += 1
                for h, r in fwd.items():
                    ch_r[ch][h].append(r)
    return pool_c, pick_c, by_channel, pool_r, pick_r, ch_r


def _row(label: str, c: collections.Counter) -> str:
    total = sum(c.values())
    cells = "".join(f"{c[k] / total * 100:>9.1f}%" if total else f"{'-':>10}" for k in KEYS)
    return f"{label:<24}{total:>8,}{cells}"


def main() -> int:
    p = argparse.ArgumentParser(prog="measure_channel_fit")
    p.add_argument("--days", type=int, default=20)
    a = p.parse_args()

    with store.connect() as conn:
        days = trading_days(conn, a.days)
        pool_c, pick_c, by_channel, pool_r, pick_r, ch_r = measure(conn, days)

    head = "".join(f"{k:>10}" for k in KEYS)
    print(f"기간 {days[0]} ~ {days[-1]} — **지표가 있는 {len(days)}일** ({', '.join(days)})\n")
    print(f"{'':<24}{'표본':>8}{head}")
    print(_row("하드필터 통과(모집단)", pool_c))
    print(_row("유니버스(AI 가 본 것)", pick_c))
    print()
    for ch in sorted(by_channel):
        print(_row(f"  채널 {ch}", by_channel[ch]))

    pt, kt = max(sum(pool_c.values()), 1), max(sum(pick_c.values()), 1)
    for k in (GOOD, SPIKE, DIST):
        a1, b1 = pool_c[k] / pt, pick_c[k] / kt
        print(f"\n{k:<8} 모집단 {a1:>6.1%} → 유니버스 {b1:>6.1%}   ({b1 - a1:+.1%}p)")
    print("\n── 이후 수익률 — **유니버스에 오른 것이 모집단보다 나은가**")
    print(fwd_header())
    print(fwd_row("하드필터(모집단)", pool_r))
    print(fwd_row("유니버스", pick_r, base=pool_r))
    for ch in sorted(ch_r):
        print(fwd_row(f"  채널 {ch}", ch_r[ch], base=pool_r))

    print(f"\n※ 채널·하드필터를 **지금 코드**로 과거에 대고 재구성했다 (상한 {ccfg.UNIVERSE_MAX})")
    print("※ 생존 편향 — 상장폐지 종목은 봉이 없다. 실제는 이보다 나쁠 것이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
