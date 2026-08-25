"""컨텍스트 팩 조립.

AI가 보는 세계 전체를 만든다. 여기 없는 정보는 판단에 반영되지 않는다.

조립 후 반드시 스키마 검증을 통과해야 한다. 통과하지 못한 팩은 AI에 보내지 않는다 —
결정 JSON을 부분 파싱하지 않는 원칙과 같다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from data import config as dcfg
from data import store
from decision import config, positions, universe

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "context_pack.schema.json"


class PackRefused(RuntimeError):
    """입력이 판단에 쓸 수 없는 상태다. AI를 호출하지 않는다."""


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # $ref 로 외부 스키마를 부르지 않도록 축약본으로 바꿔 뒀다(설계 3.1)
    return Draft202012Validator(schema)


# ── 시장 상태 ───────────────────────────────────────────


def _index_state(conn: sqlite3.Connection, symbol: str) -> dict | None:
    rows = conn.execute(
        "SELECT date, close FROM ohlcv WHERE code=? AND halted=0 ORDER BY date DESC LIMIT 21",
        (symbol,),
    ).fetchall()
    if not rows:
        return None
    closes = [r[1] for r in rows]
    cur = closes[0]
    out: dict = {"value": cur}
    if len(closes) >= 2 and closes[1]:
        out["change_pct"] = round((cur - closes[1]) / closes[1] * 100, 2)
    if len(closes) >= 21:
        ma20 = sum(closes[:20]) / 20
        if ma20:
            out["ma20_pct"] = round((cur - ma20) / ma20 * 100, 2)
    return out


def _market_block(conn: sqlite3.Connection, cycle: str) -> dict:
    session = {
        "premarket": "PRE",
        "midday": "REGULAR",
        "preclose": "REGULAR",
        "postmarket": "CLOSED",
        "event": "REGULAR",
    }[cycle]
    out: dict = {"session": session}
    for key, sym in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        st = _index_state(conn, sym)
        if st:
            out[key] = st
    return out


# ── 브리핑 ──────────────────────────────────────────────


def _briefings_block(conn: sqlite3.Connection, generated_at: datetime, cycle: str) -> list[dict]:
    """유효 브리핑의 축약본. 원문 섹션은 넣지 않는다 — 팩이 수만 토큰으로 부푼다."""
    cutoff = (generated_at - timedelta(hours=config.BRIEFING_MAX_AGE_HOURS)).isoformat()
    fresh_kinds = set(config.CYCLE_FRESH_KINDS.get(cycle, ()))
    # "새 정보"의 기준은 당일 여부가 아니라 '직전 판단 이후에 나왔는가' 다.
    # premarket(08:20)에서 새로 반영되는 kr-close-deep 은 전일 18:00 이므로
    # 당일로 조건을 걸면 영원히 false 가 된다.
    fresh_cutoff = (generated_at - timedelta(hours=config.FRESH_WINDOW_HOURS)).isoformat()

    rows = conn.execute(
        """SELECT briefing_id, kind, published_at, market, summary, parse_warnings
           FROM briefings
           WHERE published_at <= ? AND published_at >= ? AND view_count >= 0
           ORDER BY published_at DESC""",
        (generated_at.isoformat(), cutoff),
    ).fetchall()

    out: list[dict] = []
    # 36시간 창에는 같은 종류가 이틀치 들어온다(전일 18:00 + 당일 18:00).
    # 낡은 쪽은 토큰만 먹고 AI가 어느 것이 최신인지 헷갈린다. 종류별 최신 1건만 싣는다.
    seen_kinds: set[str] = set()
    for bid, kind, pub, market, summary, warns in rows:
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        views = [
            {
                "code": r[0],
                "symbol": r[1],
                "name": r[2],
                "market": r[3],
                "stance": r[4],
                "stance_inherited": bool(r[5]),
                "confidence": r[6],
                "catalyst": r[7],
                "reasons": json.loads(r[8]) if r[8] else [],
                "invalidation": r[9],
                "check_at": r[10],
                "kr_links": json.loads(r[11]) if r[11] else [],
            }
            for r in conn.execute(
                """SELECT code,symbol,name,market,stance,stance_inherited,confidence,catalyst,
                          reasons,invalidation,check_at,kr_links
                   FROM briefing_views WHERE briefing_id=? ORDER BY seq""",
                (bid,),
            ).fetchall()
        ]
        out.append(
            {
                "briefing_id": bid,
                "kind": kind,
                "published_at": pub,
                "market": market,
                "is_fresh": kind in fresh_kinds and pub > fresh_cutoff,
                "summary": summary,
                "parse_warnings": json.loads(warns) if warns else [],
                "views": views,
            }
        )
    return out


# ── 데이터 신선도 ───────────────────────────────────────


def refuse_if_stale(conn: sqlite3.Connection, as_of: date) -> str:
    """입력이 낡았으면 아무 작업도 하기 전에 멈춘다.

    유니버스 구축보다 먼저 부른다 — 낡은 가격으로 스크리닝하면 결과 전체가 무의미하다.
    """
    row = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()
    ohlcv_as_of = row[0] if row and row[0] else None
    if not ohlcv_as_of:
        raise PackRefused("일봉 데이터가 없다. `python -m data.pipeline ohlcv` 를 먼저 실행하라.")
    # 달력일로 세면 매주 월요일 아침이 거부된다 — 금요일 배치 이후 3일이 지나기 때문이다.
    # 실제로 낡았는지는 "그 사이에 장이 몇 번 섰는가"로 봐야 한다 (점검 2026-08-23 치명 E).
    stale = _sessions_missed(conn, ohlcv_as_of, as_of)
    if stale > config.MAX_OHLCV_STALE_SESSIONS:
        raise PackRefused(
            f"일봉 최신일이 {ohlcv_as_of} 로 거래일 {stale}회분 낡았다 "
            f"(허용 {config.MAX_OHLCV_STALE_SESSIONS}회). 낡은 가격으로 판단하지 않는다."
        )
    return ohlcv_as_of


def _sessions_missed(conn: sqlite3.Connection, last: str, as_of: date) -> int:
    """마지막 적재일 이후 지나간 거래일 수.

    지수(KOSPI/KOSDAQ)는 휴장일에 봉이 없으므로 거래일 달력 노릇을 한다.
    지수마저 없으면 달력일로 물러서되, 주말 2일은 빼고 센다.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM ohlcv WHERE code IN (?, ?) AND date > ? AND date <= ?",
        (*dcfg.INDEX_SYMBOLS.values(), last, as_of.isoformat()),
    ).fetchone()
    if row and row[0]:
        return int(row[0])
    days = (as_of - date.fromisoformat(last)).days
    weekends = sum(
        1
        for i in range(1, days + 1)
        if (date.fromisoformat(last) + timedelta(days=i)).weekday() >= 5
    )
    return max(0, days - weekends)


def check_coverage(conn: sqlite3.Connection) -> dict:
    """유니버스 모집단이 실제로 얼마나 채워져 있는지 본다.

    유니버스 구축보다 먼저 부른다. 하드 필터 통과 종목 수는 절단된 모수 위에서 세면
    멀쩡해 보이므로 아무것도 잡아내지 못한다 — 실제로 후보 637종목 중 314종목만 적재된
    상태에서 205를 세고 조용히 통과했다 (점검 2026-08-22 결함 2·4).

    반환값은 data_quality 에 그대로 실린다. AI가 자기 시야가 얼마나 좁은지 알아야 한다.
    """
    expected, covered = store.universe_coverage(conn, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    if expected == 0:
        raise PackRefused(
            "유니버스 모집단이 비어 있다. `python -m data.pipeline listing` 을 먼저 실행하라."
        )
    pct = covered / expected
    if pct < config.UNIVERSE_COVERAGE_REFUSE:
        raise PackRefused(
            f"유니버스 커버리지 {pct:.0%} ({covered}/{expected}종목) — "
            f"허용 하한 {config.UNIVERSE_COVERAGE_REFUSE:.0%}. "
            "모집단의 상당 부분이 데이터 없이 빠져 있어 상위 종목 선정 자체가 무의미하다. "
            "`python -m data.pipeline daily` 로 적재를 채워라."
        )
    return {"expected": expected, "covered": covered, "pct": round(pct, 4)}


def _data_quality(
    conn: sqlite3.Connection,
    as_of: date,
    briefings: list[dict],
    cycle: str,
    ohlcv_as_of: str,
    coverage: dict,
) -> dict:
    warnings: list[str] = []

    row = conn.execute("SELECT MAX(date) FROM flows").fetchone()
    flows_as_of = row[0] if row and row[0] else None
    if flows_as_of:
        fstale = (as_of - date.fromisoformat(flows_as_of)).days
        if fstale > config.MAX_FLOWS_STALE_DAYS:
            warnings.append(f"수급 데이터가 {fstale}일 낡음 (최신 {flows_as_of})")
    else:
        warnings.append("수급 데이터 없음 — flow 채널이 비어 있다")

    have = {b["kind"] for b in briefings}
    missing = [k for k in config.CYCLE_FRESH_KINDS.get(cycle, ()) if k not in have]
    if missing:
        warnings.append(f"이번 사이클 브리핑 결손: {', '.join(missing)}")

    total_warn = sum(len(b["parse_warnings"]) for b in briefings)
    if total_warn > config.MAX_PARSE_WARNINGS:
        warnings.append(f"브리핑 파싱 경고 {total_warn}건 — 관점 데이터 신뢰도가 낮다")

    unmapped = sum(
        1 for b in briefings for v in b["views"] if v["market"] == "KR" and not v["code"]
    )
    if unmapped:
        warnings.append(
            f"종목코드 매핑 실패 관점 {unmapped}건 — 유니버스 브리핑 채널에 반영되지 않았다"
        )

    unknown = store.managed_unknown_count(conn, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    if unknown:
        warnings.append(
            f"관리종목 판정 불가 {unknown}종목 — 이들은 '정상'이 아니라 '모름'이다. "
            "하드 필터가 걸러내지 못했을 수 있다"
        )

    disc_since, _ = store.disclosure_span(conn)
    if not disc_since:
        warnings.append(
            "공시 데이터 없음 — 상장폐지·불성실공시 종목이 유니버스에서 걸러지지 않는다"
        )

    if coverage["pct"] < config.UNIVERSE_COVERAGE_WARN:
        warnings.append(
            f"유니버스 커버리지 {coverage['pct']:.0%} "
            f"({coverage['covered']}/{coverage['expected']}종목) — "
            "모집단 일부가 데이터 없이 빠져 있다. 상위 종목이 실제 상위가 아닐 수 있다"
        )

    return {
        "ohlcv_as_of": ohlcv_as_of,
        "flows_as_of": flows_as_of,
        "missing_briefings": missing,
        "universe_coverage": coverage,
        "disclosures_since": disc_since,
        "warnings": warnings,
    }


# ── 조립 ────────────────────────────────────────────────


def estimate_tokens(payload: dict) -> int:
    return int(len(json.dumps(payload, ensure_ascii=False)) / config.CHARS_PER_TOKEN)


def build(
    conn: sqlite3.Connection,
    *,
    cycle: str,
    generated_at: datetime | None = None,
    event_trigger: dict | None = None,
) -> dict:
    if cycle not in config.CYCLES:
        raise ValueError(f"알 수 없는 사이클: {cycle}")

    now = generated_at or datetime.now(dcfg.KST)
    as_of = now.date()

    if not conn.execute("SELECT COUNT(*) FROM listing").fetchone()[0]:
        raise PackRefused(
            "종목 마스터가 비어 있다. `python -m data.pipeline listing` 을 먼저 실행하라."
        )

    ohlcv_as_of = refuse_if_stale(conn, as_of)
    # 유니버스를 만들기 전에 모집단이 채워져 있는지 본다.
    # 절단된 모수 위에서 랭킹하면 "상위 60종목"이 상위가 아니게 된다 (결함 2).
    coverage = check_coverage(conn)

    seed = config.account_seed()
    # 회계 항등식으로 계산한다. total_equity 를 상수로 두면 실현손실이 계좌에서 사라지고,
    # 평가금을 빼서 현금을 구하면 손실이 매수 여력으로 둔갑한다 (치명 A).
    acct = positions.account_state(conn, seed["total_equity_krw"])
    total_equity = acct["total_equity_krw"]
    pos = positions.load_open(conn, as_of, total_equity)
    held_codes = {p["code"] for p in pos}

    uni = universe.build(conn, as_of, now=now, exclude=held_codes)
    if len(uni.candidates) < config.MIN_UNIVERSE_SIZE:
        raise PackRefused(
            f"유니버스가 {len(uni.candidates)}종목뿐이다 (최소 {config.MIN_UNIVERSE_SIZE}). "
            "시장에 후보가 없는 게 아니라 스크리닝이 깨진 것으로 본다."
        )

    briefings = _briefings_block(conn, now, cycle)
    dq = _data_quality(conn, as_of, briefings, cycle, ohlcv_as_of, coverage)
    dq["warnings"].extend(uni.warnings)

    constraints = config.constraints()
    limit = constraints.get("daily_loss_limit_krw") or 0

    stamp = now.strftime("%Y%m%d-%H%M")
    pack: dict = {
        "pack_id": f"{stamp}-{cycle}",
        "generated_at": now.isoformat(timespec="seconds"),
        "cycle": cycle,
        "market": _market_block(conn, cycle),
        "account": {
            "total_equity_krw": total_equity,
            "cash_available_krw": acct["cash_available_krw"],
            "realized_pnl_today_krw": positions.realized_pnl_on(conn, as_of),
            "unrealized_pnl_krw": positions.unrealized_pnl(conn),
            "is_mock": seed["is_mock"],
        },
        "positions": pos,
        "universe": [c.to_pack_item() for c in uni.candidates],
        "briefings": briefings,
        "recent_decisions": [],
        "constraints": {
            **constraints,
            # 하드코딩된 상수였다. 환경변수로 한도를 정성껏 주입해도 아무 효과가 없었다.
            "daily_loss_limit_hit": bool(
                limit and positions.realized_pnl_on(conn, as_of) <= -abs(limit)
            ),
            "blocked_codes": positions.blocked_codes_on(conn, as_of),
        },
        "data_quality": dq,
    }
    if event_trigger:
        pack["event_trigger"] = event_trigger

    est = estimate_tokens(pack)
    if est > config.MAX_PACK_TOKENS:
        dq["warnings"].append(f"팩 추정 {est:,} 토큰 — 상한 {config.MAX_PACK_TOKENS:,} 초과")
        log.warning("팩이 토큰 상한을 넘었다. 채널 정원을 줄이는 것을 검토하라.")

    errors = sorted(_validator().iter_errors(pack), key=lambda e: e.path)
    if errors:
        msg = "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])
        raise PackRefused(f"스키마 검증 실패 — {msg}")

    return pack


def save(conn: sqlite3.Connection, pack: dict) -> None:
    conn.execute(
        """INSERT INTO context_packs
           (pack_id,cycle,generated_at,universe_size,position_count,view_count,
            warning_count,est_tokens,payload)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(pack_id) DO UPDATE SET
             generated_at=excluded.generated_at, universe_size=excluded.universe_size,
             position_count=excluded.position_count, view_count=excluded.view_count,
             warning_count=excluded.warning_count, est_tokens=excluded.est_tokens,
             payload=excluded.payload""",
        (
            pack["pack_id"],
            pack["cycle"],
            pack["generated_at"],
            len(pack["universe"]),
            len(pack["positions"]),
            sum(len(b["views"]) for b in pack.get("briefings", [])),
            len(pack.get("data_quality", {}).get("warnings", [])),
            estimate_tokens(pack),
            json.dumps(pack, ensure_ascii=False),
        ),
    )
