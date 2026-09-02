"""판단 비용 — **재지 않으면 줄일 곳을 모른다.**

## 왜 캐시 적중률이 먼저인가

입력이 비용의 대부분이다(2026-09-02 실측 77%). 그런데 입력 중 캐시로 싸게 온 몫은
정가의 **1/10** 이라, 적중률을 모르면 같은 토큰 수가 열 배까지 차이난다.
팩부터 줄이면 이미 싸게 오던 부분을 깎는 것일 수도 있다.

## 단가는 코드가 아니라 설정이다

모델 가격은 우리가 정하는 값이 아니고 바뀐다. 하드코딩하면 조용히 틀린 금액을
보고하게 된다. **모르는 모델은 금액을 내지 않고 토큰만 낸다** — 추측한 단가로
계산한 숫자는 없느니만 못하다.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from data import config as dcfg

# USD / 1M tokens. developers.openai.com/api/docs/pricing (확인 2026-09-02)
PRICES_PATH = Path(os.getenv("AIK_PRICES_FILE", str(dcfg.DATA_DIR / "model_prices.json")))
BUILTIN: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 4.00, "cached_input": 0.40, "output": 20.00},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
}
# 별칭. `gpt-5.6` 로 요청하면 응답 model 은 `gpt-5.6-sol` 이다 (API 실측 2026-09-02).
ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


def prices() -> dict[str, dict[str, float]]:
    """설정 파일이 있으면 그것이 정본이다. 없으면 내장표."""
    out = dict(BUILTIN)
    if PRICES_PATH.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            out.update(json.loads(PRICES_PATH.read_text(encoding="utf-8")))
    return out


@dataclass(frozen=True)
class Usage:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int | None  # None = 재지 못했다 (0 과 다르다)

    @property
    def cache_hit_pct(self) -> float | None:
        if self.cached_tokens is None or not self.input_tokens:
            return None
        return round(self.cached_tokens / self.input_tokens * 100, 1)

    def usd(self) -> float | None:
        """단가를 모르면 **금액을 내지 않는다.**"""
        p = prices().get(ALIASES.get(self.model, self.model))
        if not p:
            return None
        cached = self.cached_tokens or 0
        fresh = max(self.input_tokens - cached, 0)
        return (
            fresh / 1e6 * p["input"]
            + cached / 1e6 * p["cached_input"]
            + self.output_tokens / 1e6 * p["output"]
        )


def by_cycle(conn: sqlite3.Connection, *, limit: int = 20) -> list[tuple[str, Usage]]:
    """팩(사이클) 하나당 두 arm 을 합친 사용량. 최근 것부터."""
    rows = conn.execute(
        """SELECT pack_id, model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                  SUM(cached_input_tokens), SUM(cached_input_tokens IS NULL)
           FROM decisions
           WHERE run_kind='live' AND input_tokens IS NOT NULL
           GROUP BY pack_id, model ORDER BY pack_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for pack_id, model, calls, i, o, cached, unknown in rows:
        # 한 호출이라도 재지 못했으면 **합계를 재지 못한 것으로 본다.**
        # 일부만 더해서 비율을 내면 실제보다 낮게 나온다.
        out.append(
            (pack_id, Usage(model or "?", calls, i or 0, o or 0, None if unknown else cached))
        )
    return out


def total(conn: sqlite3.Connection, *, since: str | None = None) -> list[Usage]:
    where = "run_kind='live' AND input_tokens IS NOT NULL"
    args: list = []
    if since:
        where += " AND substr(generated_at,1,10) >= ?"  # date() 는 KST 를 UTC 로 옮긴다
        args.append(since)
    rows = conn.execute(
        f"""SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                   SUM(cached_input_tokens), SUM(cached_input_tokens IS NULL)
            FROM decisions WHERE {where} GROUP BY model""",
        args,
    ).fetchall()
    return [
        Usage(m or "?", c, i or 0, o or 0, None if unknown else cached)
        for m, c, i, o, cached, unknown in rows
    ]


__all__ = ["ALIASES", "BUILTIN", "Usage", "by_cycle", "prices", "total"]
