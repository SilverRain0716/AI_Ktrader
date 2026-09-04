"""거래대금 폭증 다음의 수익률을 잰다 — `prompts/decision_v8.md` 표의 근거.

**프롬프트에 숫자만 적고 재현 방법을 안 남기면 나중에 확인할 수 없다.** 값이 바뀌었는지,
표본이 늘어 결론이 달라졌는지 물으려면 같은 계산을 다시 돌릴 수 있어야 한다.

    python scripts/measure_volume_spike.py

## 아는 한계

- **생존 편향.** 상장폐지 종목은 봉이 없다. 폭증 후 사라진 종목이 빠져 있으므로
  실제 결과는 여기 숫자보다 **나쁠 것**이다 (ADR 0005 선결 과제).
- 수수료·세금·슬리피지를 빼지 않은 순수 가격 수익률이다.
- 거래대금이 아니라 **거래량** 배수다. 가격이 크게 변한 날은 둘이 갈린다.
"""

from __future__ import annotations

import collections
import statistics as st

from data import store

HORIZONS = (1, 5, 10, 20)
LOOKBACK = 20


def _load(conn) -> dict[str, list[tuple[str, int, int]]]:
    # 거래정지일(open=0·volume=0)은 지표를 오염시킨다 — CLAUDE.md 데이터 함정
    rows = conn.execute(
        "SELECT code,date,close,volume FROM ohlcv WHERE open>0 AND volume>0 ORDER BY code,date"
    ).fetchall()
    by: dict[str, list] = collections.defaultdict(list)
    for code, d, close, vol in rows:
        by[code].append((d, close, vol))
    return by


def measure(conn) -> tuple[dict, dict]:
    by = _load(conn)
    buckets: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    base: dict = collections.defaultdict(list)
    for seq in by.values():
        n = len(seq)
        for i in range(LOOKBACK, n - 1):
            close, prev = seq[i][1], seq[i - 1][1]
            avg = sum(x[2] for x in seq[i - LOOKBACK : i]) / LOOKBACK
            if not avg or not prev:
                continue
            vr = seq[i][2] / avg
            chg = (close / prev - 1) * 100
            for h in HORIZONS:
                if i + h >= n:
                    continue
                r = (seq[i + h][1] / close - 1) * 100
                base[h].append(r)
                if vr >= 3 and chg >= 5:
                    buckets["거래량 3배+ · 급등(+5%↑)"][h].append(r)
                if vr >= 3 and -2 < chg < 2:
                    buckets["거래량 3배+ · 제자리(±2%)"][h].append(r)
                if vr >= 5 and -2 < chg < 2:
                    buckets["거래량 5배+ · 제자리 ★물량소화"][h].append(r)
                if 1.0 <= vr < 1.8 and 1 <= chg < 4:
                    buckets["거래량 완만 · 완만상승"][h].append(r)
    return base, buckets


def main() -> int:
    with store.connect() as conn:
        base, buckets = measure(conn)

    head = "".join(f"{'+' + str(h) + '일':>10}" for h in HORIZONS)
    print(f"{'조건 (중앙 수익률)':<30}{'표본':>9}{head}")

    def row(label: str, d: dict) -> None:
        cells = "".join(f"{st.median(d[h]):>9.2f}%" if d.get(h) else f"{'-':>10}" for h in HORIZONS)
        print(f"{label:<28}{len(d[HORIZONS[0]]):>9,}{cells}")

    row("전체 (기준선)", base)
    for k in sorted(buckets):
        row(k, buckets[k])
    print("\n※ 생존 편향 있음 — 상장폐지 종목은 봉이 없다. 실제는 이보다 나쁠 것이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
