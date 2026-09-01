"""판단 통계 — **abstain 이 옳았는지까지 본다.**

## 비율만으로는 아무것도 못 정한다

abstain 비율이 높다는 것 자체는 좋지도 나쁘지도 않다. 시장에 기회가 없으면 안 사는 것이 맞다.
갈라야 하는 것은 **"기회가 없어서 안 샀는가"** 와 **"기회가 있었는데 못 봤는가"** 다.

그래서 abstain 한 사이클 **이후** 유니버스가 어떻게 움직였는지를 함께 잰다.

- abstain 뒤 유니버스가 올랐다 → **기회를 놓쳤다**
- abstain 뒤 유니버스가 빠졌다 → **피한 것이 맞다**

## 임계를 두지 않는다

"abstain 50% 넘으면 경고" 같은 값을 여기 박지 않는다. [ADR 0009](../docs/adr/0009-entry-timing.md)·
[ADR 0011](../docs/adr/0011-event-scan.md) 에서 고정 임계를 두 번 틀렸다. **드러내고 사람이 본다.**

## 프롬프트 버전으로 가른다

`prompt_id` 가 결정 행에 봉인돼 있다([ADR 0007](../docs/adr/0007-judgment-engine.md)).
프롬프트가 바뀌면 다른 함수이므로 이어 붙이면 안 된다 — v1 이 산 것과 v4 가 안 산 것을
같은 표본으로 세면 둘 다 해석 불가가 된다.
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
from dataclasses import dataclass

# 사후 성과를 볼 구간(거래일). 스윙이 1~30일이므로 그 안에서 잡는다.
HORIZONS = (5, 10, 20)


@dataclass(frozen=True)
class Row:
    prompt_id: str
    arm: int
    total: int
    abstain: int

    @property
    def rate(self) -> float:
        return self.abstain / self.total * 100 if self.total else 0.0


def rates(conn: sqlite3.Connection, *, run_kind: str = "live") -> list[Row]:
    """프롬프트·arm 별 abstain 비율. **재시도는 세지 않는다** — 마지막 시도만이 결정이다."""
    sql = """
        SELECT prompt_id, arm,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'abstain' THEN 1 ELSE 0 END) AS abstained
        FROM (
            SELECT decision_id, prompt_id, arm, status,
                   ROW_NUMBER() OVER (PARTITION BY decision_id ORDER BY attempt DESC) AS rn
            FROM decisions WHERE run_kind = ? AND status IN ('ok', 'abstain')
        ) WHERE rn = 1
        GROUP BY prompt_id, arm ORDER BY prompt_id, arm
    """
    return [Row(p or "(없음)", a, t, x) for p, a, t, x in conn.execute(sql, (run_kind,))]


def _forward(conn: sqlite3.Connection, codes: list[str], day: str, n: int) -> float | None:
    """`day` 종가 대비 `n` 거래일 뒤 종가의 **중앙 수익률(%)**.

    평균이 아니라 중앙값을 쓴다 — 한 종목의 급등이 유니버스 전체를 대표하면 안 된다.
    """
    out = []
    for code in codes:
        rows = conn.execute(
            "SELECT close FROM ohlcv WHERE code=? AND date>=? AND volume>0 ORDER BY date LIMIT ?",
            (code, day, n + 1),
        ).fetchall()
        if len(rows) == n + 1 and rows[0][0]:
            out.append((rows[-1][0] - rows[0][0]) / rows[0][0] * 100)
    return st.median(out) if out else None


def opportunity(conn: sqlite3.Connection, *, run_kind: str = "live") -> list[dict]:
    """abstain 한 판단 뒤 유니버스가 어떻게 움직였는가.

    **팩의 유니버스를 쓴다** — 그 시점에 실제로 볼 수 있었던 후보들이다.
    시장 지수를 쓰면 "그날 시장이 어땠나"가 되지 "그 후보들이 어땠나"가 아니다.
    """
    out = []
    for did, arm, pid, pack_id in conn.execute(
        "SELECT decision_id, arm, prompt_id, pack_id FROM decisions "
        "WHERE run_kind = ? AND status = 'abstain' GROUP BY decision_id",
        (run_kind,),
    ):
        row = conn.execute(
            "SELECT payload FROM context_packs WHERE pack_id = ?", (pack_id,)
        ).fetchone()
        if not row:
            continue
        pack = json.loads(row[0])
        day = (pack.get("data_quality") or {}).get("ohlcv_as_of")
        codes = [u["code"] for u in pack.get("universe", [])]
        if not (day and codes):
            continue
        out.append(
            {
                "decision_id": did,
                "arm": arm,
                "prompt_id": pid,
                "as_of": day,
                "universe": len(codes),
                **{f"r{n}": _forward(conn, codes, day, n) for n in HORIZONS},
            }
        )
    return out


__all__ = ["HORIZONS", "Row", "opportunity", "rates"]
