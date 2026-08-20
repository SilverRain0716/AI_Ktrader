"""일일 데이터 배치.

사용:
    python -m data.pipeline listing              # 종목 마스터 + 상장폐지 이력 갱신
    python -m data.pipeline ohlcv --limit 50     # 일봉 적재 (시총 상위 N)
    python -m data.pipeline ohlcv --full         # 전체 기간 재적재
    python -m data.pipeline flows --limit 50     # 종목별 기관·외국인 수급
    python -m data.pipeline disclosures --days 5 # DART 공시 수집
    python -m data.pipeline indicators           # 지표 계산
    python -m data.pipeline daily --limit 300    # 위를 순서대로 (운영 배치)
    python -m data.pipeline status               # 적재 현황

운영 시각: 한국시간 18:30 이후 (장 마감 + 수급 확정 반영 여유).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from data import config, indicators, store
from data.sources import dart, naver
from data.sources import listing as listing_src

log = logging.getLogger("pipeline")


def _now() -> str:
    return datetime.now(config.KST).isoformat(timespec="seconds")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ── 태스크 ──────────────────────────────────────────────


def task_listing(conn) -> None:
    started = _now()
    listed = listing_src.fetch_listed()
    n = store.replace_listing(conn, listed, updated_at=started)
    store.log_ingest(conn, started_at=started, task="listing", target=None, status="ok", rows=n)
    log.info("종목 마스터 %d건", n)

    delisted = listing_src.fetch_delisted()
    m = store.upsert_delisting(conn, delisted)
    store.log_ingest(conn, started_at=started, task="delisting", target=None, status="ok", rows=m)
    log.info("상장폐지 이력 %d건 (생존편향 방지용)", m)


def task_ohlcv(conn, *, limit: int | None, full: bool, include_index: bool = True) -> None:
    started = _now()

    targets: list[str] = []
    if include_index:
        targets.extend(config.INDEX_SYMBOLS.values())

    codes = store.tradable_codes(conn)
    if not codes:
        log.error("종목 마스터가 비어 있다. 먼저 `listing`을 실행하라.")
        return
    targets.extend(codes[:limit] if limit else codes)

    ok = fail = 0
    for i, sym in enumerate(targets, 1):
        try:
            if full:
                start = config.DEFAULT_HISTORY_START
            else:
                last = store.last_ohlcv_date(conn, sym)
                # 마지막 적재일 며칠 전부터 다시 받아 정정분을 덮어쓴다.
                start = (last - timedelta(days=7)) if last else config.default_start_for(False)

            df = naver.fetch_ohlcv(sym, start, config.today_kst())
            rows = store.upsert_ohlcv(conn, sym, df)

            halted = int(df["halted"].sum()) if not df.empty else 0
            detail = f"거래정지 {halted}일" if halted else None
            store.log_ingest(
                conn,
                started_at=started,
                task="ohlcv",
                target=sym,
                status="ok",
                rows=rows,
                detail=detail,
            )
            ok += 1
            if i % 50 == 0:
                log.info("일봉 진행 %d/%d", i, len(targets))
        except Exception as e:
            fail += 1
            log.warning("일봉 실패 %s: %s", sym, e)
            store.log_ingest(
                conn,
                started_at=started,
                task="ohlcv",
                target=sym,
                status="fail",
                detail=str(e)[:500],
            )

    log.info("일봉 완료 — 성공 %d / 실패 %d", ok, fail)
    if fail and fail > len(targets) * 0.1:
        log.error(
            "실패율 %.0f%% — 네이버가 차단했을 가능성이 있다. 확인 필요.", fail / len(targets) * 100
        )


def task_flows(conn, *, limit: int | None, pages: int) -> None:
    started = _now()
    codes = store.tradable_codes(conn)
    targets = codes[:limit] if limit else codes

    ok = fail = 0
    for i, code in enumerate(targets, 1):
        try:
            df = naver.fetch_investor_flows(code, pages=pages)
            rows = store.upsert_flows(conn, code, df, source="naver")
            store.log_ingest(
                conn, started_at=started, task="flows", target=code, status="ok", rows=rows
            )
            ok += 1
            if i % 50 == 0:
                log.info("수급 진행 %d/%d", i, len(targets))
        except Exception as e:
            fail += 1
            log.warning("수급 실패 %s: %s", code, e)
            store.log_ingest(
                conn,
                started_at=started,
                task="flows",
                target=code,
                status="fail",
                detail=str(e)[:500],
            )

    log.info("수급 완료 — 성공 %d / 실패 %d", ok, fail)


def task_disclosures(conn, *, days: int) -> None:
    """최근 N일 DART 공시.

    공시는 언론 기사가 아니라 원문에서 확보한다. 기사에 실리지 않는 공시를 놓치지 않기 위해서다.
    인증키가 없으면 배치 전체를 죽이지 않고 이 태스크만 건너뛴다.
    """
    started = _now()
    try:
        dart._api_key()
    except dart.DartKeyMissing as e:
        log.warning("공시 수집 건너뜀 — %s", e)
        store.log_ingest(
            conn,
            started_at=started,
            task="disclosures",
            target=None,
            status="skip",
            detail=str(e)[:500],
        )
        return

    today = config.today_kst()
    total = material = 0
    for offset in range(days):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:  # 주말에는 공시가 없다
            continue
        try:
            df = dart.fetch_disclosures(day)
            rows = store.upsert_disclosures(conn, df)
            n_material = int(df["material"].sum()) if not df.empty else 0
            total += rows
            material += n_material
            store.log_ingest(
                conn,
                started_at=started,
                task="disclosures",
                target=day.isoformat(),
                status="ok",
                rows=rows,
                detail=f"주요 {n_material}건",
            )
            log.info("공시 %s — 전체 %d건 / 주요 %d건", day, rows, n_material)
        except Exception as e:
            log.warning("공시 실패 %s: %s", day, e)
            store.log_ingest(
                conn,
                started_at=started,
                task="disclosures",
                target=day.isoformat(),
                status="fail",
                detail=str(e)[:500],
            )

    log.info("공시 완료 — 누적 %d건 (주요 %d건)", total, material)


def task_indicators(conn, *, limit: int | None) -> None:
    started = _now()

    benchmarks = {name: store.load_ohlcv(conn, sym) for name, sym in config.INDEX_SYMBOLS.items()}
    for name, bm in benchmarks.items():
        if bm.empty:
            log.warning("벤치마크 %s 데이터 없음 — 해당 시장 종목의 rs20은 계산되지 않는다", name)

    rows = conn.execute(
        "SELECT code, market, market_cap FROM listing WHERE is_preferred=0 AND is_spac=0 "
        "ORDER BY market_cap DESC"
    ).fetchall()
    if limit:
        rows = rows[:limit]

    ok = skip = 0
    for code, market, mcap in rows:
        df = store.load_ohlcv(conn, code)
        if df.empty:
            skip += 1
            continue

        ind = indicators.compute(
            df,
            benchmark=benchmarks.get(market),
            market_cap_krw=mcap,
        )
        flows_df = _load_flows(conn, code)
        payload = {
            "indicators": ind.to_dict(),
            "flows": indicators.compute_flows(flows_df, df["close"]),
            "bars": len(df),
        }
        store.upsert_indicators(
            conn, code, df["date"].iloc[-1], json.dumps(payload, ensure_ascii=False)
        )
        ok += 1

    store.log_ingest(
        conn,
        started_at=started,
        task="indicators",
        target=None,
        status="ok",
        rows=ok,
        detail=f"데이터 없어 건너뜀 {skip}",
    )
    log.info("지표 완료 — 계산 %d / 건너뜀 %d", ok, skip)


def _load_flows(conn, code: str):
    import pandas as pd

    df = pd.read_sql_query(
        "SELECT date, inst_net_qty, foreign_net_qty, foreign_hold_qty, foreign_hold_pct "
        "FROM flows WHERE code=? ORDER BY date",
        conn,
        params=(code,),
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def task_status(conn) -> None:
    c = store.counts(conn)
    print("── 적재 현황 ──")
    for k, v in c.items():
        print(f"  {k:12s} {v:>10,}")

    row = conn.execute("SELECT MIN(date), MAX(date) FROM ohlcv").fetchone()
    if row and row[0]:
        print(f"  일봉 기간     {row[0]} ~ {row[1]}")

    print("\n── 최근 배치 (실패만) ──")
    fails = conn.execute(
        "SELECT started_at, task, target, detail FROM ingest_log "
        "WHERE status='fail' ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not fails:
        print("  실패 없음")
    for r in fails:
        print(f"  {r[0]} {r[1]:10s} {r[2] or '':8s} {(r[3] or '')[:80]}")


# ── 진입점 ──────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="data.pipeline", description="일일 데이터 배치")
    p.add_argument(
        "task",
        choices=["listing", "ohlcv", "flows", "disclosures", "indicators", "daily", "status"],
    )
    p.add_argument("--limit", type=int, default=None, help="대상 종목 수 (시총 상위)")
    p.add_argument("--full", action="store_true", help="전체 기간 재적재")
    p.add_argument("--pages", type=int, default=1, help="수급 페이지 수 (1페이지=20영업일)")
    p.add_argument("--days", type=int, default=5, help="공시 수집 대상 일수 (오늘부터 역순)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    with store.connect() as conn:
        store.init_db(conn)

        if args.task == "listing":
            task_listing(conn)
        elif args.task == "ohlcv":
            task_ohlcv(conn, limit=args.limit, full=args.full)
        elif args.task == "flows":
            task_flows(conn, limit=args.limit, pages=args.pages)
        elif args.task == "disclosures":
            task_disclosures(conn, days=args.days)
        elif args.task == "indicators":
            task_indicators(conn, limit=args.limit)
        elif args.task == "status":
            task_status(conn)
        elif args.task == "daily":
            task_listing(conn)
            task_ohlcv(conn, limit=args.limit, full=args.full)
            task_flows(conn, limit=args.limit, pages=args.pages)
            task_disclosures(conn, days=args.days)
            task_indicators(conn, limit=args.limit)
            task_status(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
