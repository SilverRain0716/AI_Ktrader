"""증거금 등급 — 사람이 내려받은 CSV 를 적재한다.

## 왜 파일인가

**증거금률은 거래소 값이 아니라 증권사가 종목별로 정하는 값**이라 시세 API 에 없다.
실제로 찾아봤고 전부 막혔다(2026-08-31).

| 시도 | 결과 |
|---|---|
| 키움 `ka10001` (45개 필드) | 없음. `crd_rt` 는 **신용비율**이지 증거금률이 아니다 |
| 네이버 종목 메인·시세·종목분석 | `증거금` 문자열 **0회** |
| 키움 증거금률 안내 페이지 | HTTP 200 이나 `증거금` **0회** |

그래서 HTS 에서 내려받은 CSV 를 정본으로 삼는다. **자동 갱신이 안 되므로 `as_of` 를
반드시 남긴다** — 등급은 수시로 바뀌고, 낡은 등급으로 우량주를 판정하면 조용히 틀린다.

## 시총으로 대신하면 안 된다

실측(182종목, 2026-08-31): 시총 3,000억 이상으로 거르면 **정밀도 90.1%**.
10종목 중 1개가 증거금 50~100% 종목이고, **그것이 원칙 1 이 배제하려던 바로 그 종목이다.**

| 종목 | 시총 | 실제 등급 |
|---|---|---|
| 삼천당제약 | 4.0조 | **증100%** |
| 두산퓨얼셀 | 3.0조 | 증50% |
| SK이터닉스 | 1.9조 | 증50% |

시총을 올려도 안 낫다 — 1조 기준으로도 정밀도 96.2% 에 재현율이 76.2% 로 떨어진다.
[ADR 0013](../../docs/adr/0013-trading-doctrine.md) 이 *"프록시라고 적고 같다고 말하지 않는다"*
로 둔 이유이고, 이 모듈이 그 프록시를 대체한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("data.margin")

# 파일명이 등급이다: "20%.csv" → 20
_RATE_FROM_NAME = re.compile(r"(\d{1,3})\s*%")
# 구분 문자열 예: "신용A/담보A/대주A/증20" · "정지/증100" · "주의/경예/증100"
_CREDIT = re.compile(r"신용([A-E])")
_COLLAT = re.compile(r"담보([A-E])")
_SHORT = re.compile(r"대주([A-E])")
_MARGIN_IN_GRADE = re.compile(r"증\s*(\d{1,3})")

VALID_RATES = (20, 30, 40, 50, 60, 100)


class MarginLoadError(RuntimeError):
    """읽을 수 없다. **빈 결과로 위장하지 않는다** — 등급이 없으면 원칙 1 이 꺼진다."""


@dataclass(frozen=True)
class MarginRow:
    code: str
    margin_pct: int
    name: str | None
    grade_raw: str
    credit: str | None
    collateral: str | None
    short_sell: str | None
    halted: bool
    caution: bool


def _read_csv(path: Path) -> list[dict]:
    """인코딩을 추측한다. HTS 저장분은 cp949 인 경우가 흔하다."""
    import csv

    last: Exception | None = None
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with path.open(encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            if rows and any("종목번호" in (k or "") for k in rows[0]):
                return rows
        except (UnicodeDecodeError, LookupError) as e:
            last = e
    raise MarginLoadError(f"{path.name}: 인코딩을 판별할 수 없거나 '종목번호' 열이 없다 ({last})")


def parse_grade(
    raw: str, fallback_pct: int
) -> tuple[int, str | None, str | None, str | None, bool, bool]:
    """구분 문자열에서 등급을 뜯는다.

    **구분 안의 `증NN` 을 파일명보다 우선한다.** 파일이 잘못 분류돼 있어도 원문이 맞다 —
    이 저장소가 반복해 당한 "겉으로 구분되지 않는 오류"를 여기서 막는다.
    """
    m = _MARGIN_IN_GRADE.search(raw)
    pct = int(m.group(1)) if m else fallback_pct
    cred = _CREDIT.search(raw)
    coll = _COLLAT.search(raw)
    shrt = _SHORT.search(raw)
    return (
        pct,
        cred.group(1) if cred else None,
        coll.group(1) if coll else None,
        shrt.group(1) if shrt else None,
        "정지" in raw,
        bool(re.search(r"주의|경고|경예|위험", raw)),
    )


def load_dir(directory: str | Path) -> list[MarginRow]:
    """`20%.csv` 같은 파일이 모인 폴더를 읽는다. **한 종목이 두 등급에 나오면 예외다.**"""
    d = Path(directory)
    files = sorted(d.glob("*.csv"))
    if not files:
        raise MarginLoadError(f"{d}: CSV 가 없다")

    out: dict[str, MarginRow] = {}
    for path in files:
        m = _RATE_FROM_NAME.search(path.stem)
        if not m:
            log.warning("파일명에서 등급을 못 읽어 건너뛴다: %s", path.name)
            continue
        fallback = int(m.group(1))
        for row in _read_csv(path):
            code = str(row.get("종목번호") or "").lstrip("'").strip()
            if not code:
                continue
            raw = str(row.get("구분") or "").lstrip("'").strip()
            pct, cred, coll, shrt, halted, caution = parse_grade(raw, fallback)
            if pct not in VALID_RATES:
                raise MarginLoadError(f"{path.name}: 알 수 없는 증거금률 {pct} (원문 {raw!r})")
            prev = out.get(code)
            if prev and prev.margin_pct != pct:
                raise MarginLoadError(
                    f"{code} 가 증{prev.margin_pct}% 와 증{pct}% 양쪽에 있다. "
                    "내려받은 파일이 서로 다른 시점의 것일 수 있다"
                )
            out[code] = MarginRow(
                code=code,
                margin_pct=pct,
                name=(str(row.get("종목명") or "").strip() or None),
                grade_raw=raw,
                credit=cred,
                collateral=coll,
                short_sell=shrt,
                halted=halted,
                caution=caution,
            )
    if not out:
        raise MarginLoadError(f"{d}: 읽었으나 종목이 한 건도 없다")
    return list(out.values())


def save(conn: sqlite3.Connection, rows: list[MarginRow], *, as_of: date | str) -> int:
    """**스냅샷으로 쌓는다. 옛 날짜를 지우지 않는다.**

    처음에 전체를 지우고 새로 넣었다. 틀렸다 — 등급은 **과거를 받아올 수 없는 소멸 원천**이라
    덮어쓰면 소급 검증이 영영 불가능해진다([ADR 0011](../../docs/adr/0011-event-scan.md) 이
    뉴스를 매일 쌓기로 한 것과 같은 이유). 대신 조회는 **최신 스냅샷만** 본다.

    같은 `as_of` 로 다시 넣으면 그 날짜만 갱신된다.
    """
    as_of = as_of.isoformat() if isinstance(as_of, date) else as_of
    conn.executemany(
        "INSERT OR REPLACE INTO margin_grades (code,margin_pct,name,grade_raw,credit,"
        "collateral,short_sell,halted,caution,as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.code,
                r.margin_pct,
                r.name,
                r.grade_raw,
                r.credit,
                r.collateral,
                r.short_sell,
                int(r.halted),
                int(r.caution),
                as_of,
            )
            for r in rows
        ],
    )
    return len(rows)


def latest_as_of(conn: sqlite3.Connection, *, on: date | str | None = None) -> str | None:
    """`on` 이하의 최신 스냅샷 날짜. 리플레이에서 미래 등급이 새는 것을 막는다."""
    if on is None:
        row = conn.execute("SELECT MAX(as_of) FROM margin_grades").fetchone()
    else:
        on = on.isoformat() if isinstance(on, date) else on
        row = conn.execute(
            "SELECT MAX(as_of) FROM margin_grades WHERE as_of <= ?", (on,)
        ).fetchone()
    return row[0] if row else None


def eligible(
    conn: sqlite3.Connection, *, max_pct: int = 40, on: date | str | None = None
) -> dict[str, int]:
    """원칙 1 대상. **등급을 모르는 종목은 포함하지 않는다.**

    모르는 것을 통과시키면 표가 잘려 있을 때 조용히 전 종목이 통과한다 — 실제로 처음
    내려받은 표는 유니버스 662 중 183종목만 덮고 있었다.

    `on` 을 주면 그 날짜 **이하**의 최신 스냅샷을 쓴다. 주지 않으면 가장 최근 것이다.
    """
    as_of = latest_as_of(conn, on=on)
    if as_of is None:
        return {}
    return {
        code: pct
        for code, pct in conn.execute(
            "SELECT code, margin_pct FROM margin_grades "
            "WHERE as_of = ? AND margin_pct <= ? AND halted = 0 AND caution = 0",
            (as_of, max_pct),
        )
    }


__all__ = [
    "MarginLoadError",
    "MarginRow",
    "eligible",
    "latest_as_of",
    "load_dir",
    "parse_grade",
    "save",
]
