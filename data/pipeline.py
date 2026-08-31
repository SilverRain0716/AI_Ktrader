"""일일 데이터 배치.

사용:
    python -m data.pipeline listing              # 종목 마스터 + 상장폐지 이력 갱신
    python -m data.pipeline ohlcv                # 일봉 적재 (시총 하한 이상 전 종목)
    python -m data.pipeline ohlcv --full         # 전체 기간 재적재
    python -m data.pipeline flows                # 종목별 기관·외국인 수급
    python -m data.pipeline disclosures --days 5 # DART 공시 수집
    python -m data.pipeline indicators           # 지표 계산
    python -m data.pipeline daily                # 위를 순서대로 (운영 배치)
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

    task_managed(conn)


def task_managed(conn) -> None:
    """관리종목 판정.

    FDR 소속부는 코스닥에만 있어 KOSPI 관리종목이 하나도 걸러지지 않았다 (치명 D).
    네이버 basic API 로 두 시장 모두 판정한다.

    **판정에 실패한 종목은 정상으로 두지 않는다.** `is_managed_known=0` 으로 표시해
    "관리종목이 아니다"와 "모른다"를 구분한다 — 둘을 섞으면 실패가 조용히 통과가 된다.
    """
    started = _now()
    rows = conn.execute(
        "SELECT code, dept FROM listing WHERE is_preferred=0 AND is_spac=0 "
        "AND market_cap >= ? ORDER BY market_cap DESC",
        (config.INGEST_MIN_MARKET_CAP_KRW,),
    ).fetchall()
    log.info("관리종목 판정 대상 %d종목", len(rows))

    hit = unknown = 0
    for i, (code, dept) in enumerate(rows, 1):
        try:
            # 두 신호는 서로를 덮어쓰지 않는다 — 잡는 것이 다르다.
            #   FDR 소속부 : 코스닥 관리종목 + **투자주의환기** (KOSPI 는 전부 결측)
            #   네이버      : 양 시장 관리종목 (투자주의환기는 안 잡는다)
            # 처음엔 네이버로 덮어썼다가 메지온·에스티큐브 등 투자주의환기 4종목을 잃었다.
            managed = listing_src.is_managed_dept(dept) or naver.fetch_is_managed(code)
            conn.execute(
                "UPDATE listing SET is_managed=?, is_managed_known=1 WHERE code=?",
                (int(managed), code),
            )
            hit += int(managed)
        except Exception as e:
            unknown += 1
            conn.execute("UPDATE listing SET is_managed_known=0 WHERE code=?", (code,))
            log.warning("관리종목 판정 실패 %s: %s", code, e)
        if i % 200 == 0:
            log.info("관리종목 진행 %d/%d", i, len(rows))

    store.log_ingest(
        conn,
        started_at=started,
        task="managed",
        target=None,
        status="ok",
        rows=hit,
        detail=f"판정불가 {unknown}",
    )
    log.info("관리종목 판정 완료 — 관리종목 %d / 판정불가 %d", hit, unknown)


def task_ohlcv(conn, *, limit: int | None, full: bool, include_index: bool = True) -> None:
    started = _now()

    targets: list[str] = []
    if include_index:
        targets.extend(config.INDEX_SYMBOLS.values())

    codes = store.tradable_codes(conn, min_market_cap=config.INGEST_MIN_MARKET_CAP_KRW)
    if not codes:
        log.error("종목 마스터가 비어 있다. 먼저 `listing`을 실행하라.")
        return
    targets.extend(codes[:limit] if limit else codes)
    log.info(
        "일봉 대상 %d종목 (시총 %.0f억 이상)%s",
        len(codes) if not limit else min(limit, len(codes)),
        config.INGEST_MIN_MARKET_CAP_EOK_KRW,
        f" — --limit {limit} 로 잘림" if limit else "",
    )

    ok = fail = 0
    failed_targets: list[str] = []
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
            failed_targets.append(sym)
            log.warning("일봉 실패 %s: %s", sym, e)
            store.log_ingest(
                conn,
                started_at=started,
                task="ohlcv",
                target=sym,
                status="fail",
                detail=str(e)[:500],
            )

    if failed_targets:
        # 일시적 실패는 한 번 더 시도한다. 실제로 SK·셀트리온·현대모비스가 이렇게 실패했고,
        # 이전 실행분이 남아 있어 드러나지 않았을 뿐이다.
        # 그래도 남는 결손은 커버리지 가드가 잡는다 — 재시도는 가드의 대체재가 아니다.
        log.info("일봉 재시도 %d종목", len(failed_targets))
        for sym in list(failed_targets):
            try:
                df = naver.fetch_ohlcv(sym, config.default_start_for(False), config.today_kst())
                rows = store.upsert_ohlcv(conn, sym, df)
                store.log_ingest(
                    conn,
                    started_at=started,
                    task="ohlcv",
                    target=sym,
                    status="ok",
                    rows=rows,
                    detail="재시도 성공",
                )
                ok += 1
                fail -= 1
                failed_targets.remove(sym)
            except Exception as e:
                log.warning("일봉 재시도 실패 %s: %s", sym, e)

    log.info("일봉 완료 — 성공 %d / 실패 %d", ok, fail)
    if fail and fail > len(targets) * 0.1:
        log.error(
            "실패율 %.0f%% — 네이버가 차단했을 가능성이 있다. 확인 필요.", fail / len(targets) * 100
        )


def task_flows(conn, *, limit: int | None, pages: int) -> None:
    started = _now()
    codes = store.tradable_codes(conn, min_market_cap=config.INGEST_MIN_MARKET_CAP_KRW)
    targets = codes[:limit] if limit else codes
    log.info(
        "수급 대상 %d종목 (시총 %.0f억 이상)%s",
        len(targets),
        config.INGEST_MIN_MARKET_CAP_EOK_KRW,
        f" — --limit {limit} 로 잘림" if limit else "",
    )

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

    # 개수가 아니라 모수 대비 비율을 본다. "몇 종목 적재됨"은 무엇이 빠졌는지 알려주지 않는다.
    expected, covered = store.universe_coverage(
        conn, min_market_cap=config.INGEST_MIN_MARKET_CAP_KRW
    )
    if expected:
        pct = covered / expected
        mark = "" if pct >= 0.95 else "  ⚠ 유니버스에 구멍이 있다"
        print(
            f"\n── 유니버스 커버리지 ──\n"
            f"  모집단 {expected:,}종목 (시총 {config.INGEST_MIN_MARKET_CAP_EOK_KRW:,.0f}억 이상) "
            f"/ 지표 확보 {covered:,} = {pct:.1%}{mark}"
        )

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


def task_ohlcv_integrated(conn, *, limit: int | None = None, base_dt: str | None = None) -> None:
    """일봉을 **통합 거래소(KRX+NXT)** 기준으로 다시 채운다.

    네이버 일봉은 KRX 만 담는다. 실측(2026-09-01, 24종목 표본):
    **우리 DB / 통합 = 중앙 75% · 최소 35% · 최대 100%**, NXT 비중이 종목마다 **0~65%** 다.

    단순 배율이 아니라 종목마다 다르므로 **거래대금 비교가 통째로 왜곡된다** —
    사장님 원칙 5(거래대금 방향)와 급변 스캔(ADR 0011)의 배수·절대 순위가 전부 그 위에 있다.

    축척은 확인했다: 키움 수정주가(`upd_stkpc_tp=1`)가 네이버와 같다.
    가온전선이 원본가와는 224/267 불일치인데 수정주가와는 0/267 일치한다.

    **지수(KOSPI·KOSDAQ)는 건드리지 않는다** — ka10081 이 지수를 1건만 준다.
    거래정지일 행도 그대로 둔다(키움이 그 날짜를 주지 않으므로 네이버 행이 남는다).
    """
    from datetime import date as _date

    from data.sources.kiwoom import KiwoomClient

    base_dt = base_dt or _date.today().strftime("%Y%m%d")
    codes = [
        r[0]
        for r in conn.execute(
            "SELECT o.code FROM ohlcv o "
            "JOIN (SELECT code FROM listing GROUP BY code) l ON l.code = o.code "
            "GROUP BY o.code ORDER BY MAX(o.close * o.volume) DESC"
            + (f" LIMIT {int(limit)}" if limit else "")
        )
    ]
    if not codes:
        log.warning("대상 종목이 없다 — 먼저 일봉을 적재한다")
        return
    since = conn.execute("SELECT MIN(date) FROM ohlcv").fetchone()[0]
    client = KiwoomClient()
    done = rows = 0
    failed: dict[str, str] = {}
    for code in codes:
        try:
            bars = client.daily_chart(code, base_dt=base_dt, venue="AL")
        except Exception as e:
            failed[code] = f"{type(e).__name__}: {e}"
            continue
        keep = [b for b in bars if b["date"] >= since]
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv "
            "(code,date,open,high,low,close,volume,halted,source,adjusted) "
            "VALUES (?,?,?,?,?,?,?,0,'kiwoom_al',1)",
            [
                (code, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"])
                for b in keep
            ],
        )
        done += 1
        rows += len(keep)
        if done % 100 == 0:
            conn.commit()
            log.info("  통합 일봉 %d/%d종목 · %d행", done, len(codes), rows)
    conn.commit()
    log.info("통합 일봉 완료 — %d/%d종목 · %d행 (실패 %d)", done, len(codes), rows, len(failed))
    if failed:
        for code, why in list(failed.items())[:3]:
            log.warning("  %s: %s", code, why[:70])
    log.warning(
        "**지표를 다시 계산해야 한다** — 종가·거래량이 바뀌었다: python -m data.pipeline indicators"
    )


def task_margin(conn, *, limit: int | None = None) -> None:
    """증거금 등급을 API 로 받는다 (ADR 0013 원칙 1).

    **API 가 정본이고 CSV 는 폴백이다.** 정확도는 같지만(10종목 대조 전부 일치)
    **신선도가 다르다** — 등급이 바뀌면 API 는 즉시 반영하고 CSV 는 사람이 다시
    내려받아야 안다. 낡은 등급으로 우량주를 판정하면 조용히 틀린다.

    실패해도 배치를 멈추지 않는다. 다만 **일부만 받고 전체인 척하지 않는다** —
    받은 것만 그날 스냅샷으로 남고, 못 받은 종목은 `eligible()` 에서 빠진다
    (등급 미상은 통과시키지 않는다).
    """
    from datetime import date as _date

    from data.sources import margin

    day = conn.execute("SELECT MAX(date) FROM ohlcv WHERE volume>0").fetchone()[0]
    if not day:
        log.warning("일봉이 없어 증거금 조회를 건너뛴다")
        return
    rows = conn.execute(
        # **`LENGTH(code)=6` 으로 지수를 거를 수 없다** — 'KOSDAQ' 이 정확히 6자다
        # (그 때문에 kt00011 이 "종목정보가 존재하지 않습니다"로 실패했다).
        #
        # 그렇다고 숫자 6자리로 좁히면 **실제 종목이 잘린다** — 삼성에피스홀딩스(0126Z0),
        # 에임드바이오(0009K0) 처럼 **보통주인데 코드에 문자가 있는** 종목이 있다.
        # 정답은 `listing` 조인이다: 지수는 상장 목록에 없고, 상장 종목은 전부 있다.
        "SELECT o.code, o.close FROM ohlcv o "
        "JOIN (SELECT code FROM listing GROUP BY code) l ON l.code = o.code "
        "WHERE o.date=? AND o.volume>0 "
        "ORDER BY o.close*o.volume DESC" + (f" LIMIT {int(limit)}" if limit else ""),
        (day,),
    ).fetchall()
    try:
        got, failed = margin.fetch_api([(c, int(p)) for c, p in rows])
    except Exception as e:
        log.error("증거금 조회 실패 — %s: %s", type(e).__name__, e)
        log.error(
            '  CSV 폴백: python -c "from data.sources import margin; ..." 또는 이전 스냅샷이 쓰인다'
        )
        return
    if not got:
        log.error("증거금을 한 건도 받지 못했다 — 우량주 필터가 이전 스냅샷으로 돈다")
        return
    margin.save(conn, got, as_of=_date.today())
    conn.commit()
    log.info("증거금 완료 — %d/%d종목 (실패 %d)", len(got), len(rows), len(failed))
    if failed:
        for code, why in list(failed.items())[:3]:
            log.warning("  %s: %s", code, why[:70])


def task_briefings(conn, *, days: int = 5) -> None:
    """GitLab 브리핑을 동기화한다.

    **`daily` 에 이것이 없어서 브리핑이 사흘 낡았다**(2026-09-01 발견). 일봉·수급·공시·지표는
    돌았지만 브리핑 동기화는 별도 명령이라 아무도 안 돌렸고, 그 사이 팩의 briefing 채널이
    비어 유니버스가 2채널로만 구성됐다. **브리핑이 없으면 F3(브리핑 기여도) 자체가
    측정 불가**다 — Arm 1 과 Arm 2 의 입력이 같아진다.

    `briefing` 패키지가 `data` 를 임포트하므로 여기서는 **지연 임포트**한다.
    모듈 최상단에 두면 순환이 된다.

    실패해도 배치 전체를 멈추지 않는다 — 브리핑은 외부 저장소이고, 일봉이 들어오는 것이
    더 중요하다. 대신 **조용히 넘어가지 않는다.**
    """
    from briefing import pipeline as bp

    try:
        bp.task_sync(conn, days=days, full=False)
    except Exception as e:
        log.error("브리핑 동기화 실패 — %s: %s", type(e).__name__, e)
        log.error(
            "  팩의 briefing 채널이 비고 F3 를 잴 수 없다. 수동 확인: python -m briefing.pipeline sync"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="data.pipeline", description="일일 데이터 배치")
    p.add_argument(
        "task",
        choices=[
            "listing",
            "ohlcv",
            "flows",
            "disclosures",
            "indicators",
            "briefings",
            "margin",
            "ohlcv-integrated",
            "daily",
            "status",
        ],
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="대상을 시총 상위 N개로 자른다. 시험용 — 운영 배치에서는 쓰지 않는다 "
        "(자르면 유니버스 모집단에 구멍이 생긴다, 결함 2)",
    )
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
        elif args.task == "briefings":
            task_briefings(conn, days=args.days)
        elif args.task == "margin":
            task_margin(conn, limit=args.limit)
        elif args.task == "ohlcv-integrated":
            task_ohlcv_integrated(conn, limit=args.limit)
        elif args.task == "daily":
            task_listing(conn)
            task_ohlcv(conn, limit=args.limit, full=args.full)
            task_flows(conn, limit=args.limit, pages=args.pages)
            task_disclosures(conn, days=args.days)
            task_briefings(conn, days=args.days)
            task_margin(conn, limit=args.limit)
            task_indicators(conn, limit=args.limit)
            task_status(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
