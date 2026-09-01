"""거래일 판정. **미래는 모른다는 것을 인정한다.**

## 왜 휴장일 API 를 쓰지 않는가

이전 프로젝트(K-Trader)는 공공데이터포털 특일 API + 로컬 캐시 + **음력 명절 하드코딩 테이블**로
풀었다. 키가 필요하고, 음력은 해마다 달라 테이블을 손으로 채워야 하며, 그 테이블이 틀리면
**조용히 틀린다** — 휴장일에 사이클이 돌거나 거래일에 안 돈다.

우리는 이미 **거래일 목록을 갖고 있다.** 지수(`KOSPI`) 일봉이 있는 날이 거래일이다.
그것은 추정이 아니라 사실이고, 음력도 임시공휴일도 대체휴일도 이미 반영돼 있다.

## 다만 미래는 모른다

지수 봉은 **장이 끝나야** 들어온다. 오늘이 거래일인지 08:20 에는 알 수 없다.

그래서 이렇게 나눈다.

- **주말**: 확실히 아니다. 요일로 판정한다
- **평일**: 거래일로 **가정하고 돈다.** 휴장일이면 데이터가 없어 팩 생성이 거부되고
  그 사실이 로그에 남는다 — **주문은 나가지 않는다**

즉 **틀리는 방향이 안전한 쪽**이다. 휴장일에 헛돌지언정 거래일을 건너뛰지 않는다.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

INDEX_CODE = "KOSPI"


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def was_trading_day(conn: sqlite3.Connection, day: date) -> bool:
    """**지난** 날이 거래일이었는가. 지수 봉이 곧 사실이다."""
    row = conn.execute(
        "SELECT 1 FROM ohlcv WHERE code=? AND date=? LIMIT 1", (INDEX_CODE, day.isoformat())
    ).fetchone()
    return row is not None


def last_trading_day(conn: sqlite3.Connection, on: date | None = None) -> date | None:
    """`on` 이하의 마지막 거래일."""
    on = on or date.today()
    row = conn.execute(
        "SELECT MAX(date) FROM ohlcv WHERE code=? AND date<=?", (INDEX_CODE, on.isoformat())
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def should_run(conn: sqlite3.Connection, day: date) -> tuple[bool, str]:
    """오늘 사이클을 돌려야 하는가. `(돌린다, 사유)`.

    **평일이면 돈다.** 휴장일이면 데이터가 없어 뒤에서 막히고 주문은 나가지 않는다 —
    거래일을 건너뛰는 것보다 헛도는 편이 안전하다.
    """
    if is_weekend(day):
        return False, f"{day} 는 주말이다"
    last = last_trading_day(conn, day - timedelta(days=1))
    if last is None:
        return True, "거래일 이력이 없다 — 첫 실행으로 본다"
    gap = (day - last).days
    if gap > 7:
        # 연휴가 길어도 7일을 넘기는 일은 드물다. 데이터가 낡았을 가능성이 더 크다.
        return True, f"직전 거래일이 {last} 로 {gap}일 전이다 — 데이터 결손 의심, 돌리되 확인하라"
    return True, f"평일 (직전 거래일 {last})"


__all__ = ["INDEX_CODE", "is_weekend", "last_trading_day", "should_run", "was_trading_day"]
