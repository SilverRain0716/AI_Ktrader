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
    stale = (as_of - date.fromisoformat(ohlcv_as_of)).days
    if stale > config.MAX_OHLCV_STALE_DAYS:
        raise PackRefused(
            f"일봉 최신일이 {ohlcv_as_of} 로 {stale}일 낡았다 "
            f"(허용 {config.MAX_OHLCV_STALE_DAYS}일). 낡은 가격으로 판단하지 않는다."
        )
    return ohlcv_as_of


def _data_quality(
    conn: sqlite3.Connection, as_of: date, briefings: list[dict], cycle: str, ohlcv_as_of: str
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

    return {
        "ohlcv_as_of": ohlcv_as_of,
        "flows_as_of": flows_as_of,
        "missing_briefings": missing,
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

    seed = config.account_seed()
    holdings = positions.holdings_value(conn)
    total_equity = seed["total_equity_krw"]
    pos = positions.load_open(conn, as_of, total_equity)
    held_codes = {p["code"] for p in pos}

    uni = universe.build(conn, as_of, exclude=held_codes)
    if len(uni.candidates) < config.MIN_UNIVERSE_SIZE:
        raise PackRefused(
            f"유니버스가 {len(uni.candidates)}종목뿐이다 (최소 {config.MIN_UNIVERSE_SIZE}). "
            "시장에 후보가 없는 게 아니라 스크리닝이 깨진 것으로 본다."
        )

    briefings = _briefings_block(conn, now, cycle)
    dq = _data_quality(conn, as_of, briefings, cycle, ohlcv_as_of)
    dq["warnings"].extend(uni.warnings)

    stamp = now.strftime("%Y%m%d-%H%M")
    pack: dict = {
        "pack_id": f"{stamp}-{cycle}",
        "generated_at": now.isoformat(timespec="seconds"),
        "cycle": cycle,
        "market": _market_block(conn, cycle),
        "account": {
            "total_equity_krw": total_equity,
            "cash_available_krw": max(0, total_equity - holdings),
            "realized_pnl_today_krw": positions.realized_pnl_on(conn, as_of),
            "unrealized_pnl_krw": positions.unrealized_pnl(conn),
            "is_mock": seed["is_mock"],
        },
        "positions": pos,
        "universe": [c.to_pack_item() for c in uni.candidates],
        "briefings": briefings,
        "recent_decisions": [],
        "constraints": {**config.constraints(), "daily_loss_limit_hit": False, "blocked_codes": []},
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
