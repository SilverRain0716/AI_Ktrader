"""컨텍스트 팩 배치.

사용:
    python -m decision.pipeline build --cycle premarket
    python -m decision.pipeline build --cycle event --code 005930 --trigger invalidation_hit
    python -m decision.pipeline decide --pack-id 20260821-0820-premarket   # Arm 1·2 짝
    python -m decision.pipeline decide --provider openai --model gpt-5.6
    python -m decision.pipeline show  --pack-id 20260821-0820-premarket
    python -m decision.pipeline status

운영 시각(KST): 08:20 premarket / 12:20 midday / 15:00 preclose / 18:30 postmarket.
각 브리핑 발행 직후에 돌린다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from data import store
from decision import config, engine, pack, providers

log = logging.getLogger("decision")


def task_build(
    conn, *, cycle: str, trigger: str | None, code: str | None, detail: str | None
) -> int:
    event = None
    if cycle == "event":
        if not trigger:
            log.error("event 사이클은 --trigger 가 필요하다")
            return 2
        event = {"kind": trigger}
        if code:
            event["code"] = code
        if detail:
            event["detail"] = detail

    try:
        p = pack.build(conn, cycle=cycle, event_trigger=event, with_news=True)
    except config.RiskLimitError as e:
        # RiskLimitError 는 PackRefused 의 하위다 — 아래보다 먼저 잡아야 구분이 된다.
        # 종료 코드를 나눈다: 3 = 설정 문제(사람이 .env 를 고쳐야 한다),
        #                     1 = 데이터 문제(배치를 돌리면 해결될 수 있다).
        log.error("리스크 한도 설정 오류 — 팩을 만들지 않는다: %s", e)
        return 3
    except pack.PackRefused as e:
        log.error("팩 생성 거부 — %s", e)
        return 1

    pack.save(conn, p)
    dq = p["data_quality"]
    log.info(
        "팩 생성 %s — 유니버스 %d / 보유 %d / 브리핑 %d(관점 %d) / 추정 %s 토큰",
        p["pack_id"],
        len(p["universe"]),
        len(p["positions"]),
        len(p["briefings"]),
        sum(len(b["views"]) for b in p["briefings"]),
        f"{pack.estimate_tokens(p):,}",
    )
    ch: dict[str, int] = {}
    for c in p["universe"]:
        for name in c["channels"]:
            ch[name] = ch.get(name, 0) + 1
    log.info("채널별 종목 수: %s", ch or "없음")
    if dq["warnings"]:
        log.warning("데이터 경고 %d건 — AI는 확신도를 낮춰야 한다:", len(dq["warnings"]))
        for w in dq["warnings"]:
            log.warning("   · %s", w)
    return 0


def task_show(conn, pack_id: str) -> int:
    row = conn.execute("SELECT payload FROM context_packs WHERE pack_id=?", (pack_id,)).fetchone()
    if not row:
        log.error("그런 팩이 없다: %s", pack_id)
        return 1
    print(json.dumps(json.loads(row[0]), ensure_ascii=False, indent=2))
    return 0


def task_status(conn) -> int:
    n = conn.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0]
    print(f"── 컨텍스트 팩 {n}건 ──")
    for row in conn.execute(
        "SELECT pack_id,cycle,universe_size,position_count,view_count,warning_count,est_tokens "
        "FROM context_packs ORDER BY generated_at DESC LIMIT 10"
    ):
        pid, cyc, u, po, v, w, t = row
        print(
            f"   {pid:<28} {cyc:<11} 유니버스{u:>3} 보유{po:>2} 관점{v:>3} 경고{w:>2} {t or 0:>7,}토큰"
        )

    print("\n── 현재 설정 ──")
    print(f"   유니버스 상한 {config.UNIVERSE_MAX} (채널 {config.CHANNEL_QUOTA})")
    print(
        f"   하드 필터: 거래대금 ≥ {config.MIN_ADV20_EOK_KRW:.0f}억 / 시총 ≥ {config.MIN_MARKET_CAP_EOK_KRW:.0f}억"
    )
    limits_ok = True
    try:
        print(f"   리스크 한도: {config.constraints()}")
        print(f"   페이퍼 시드: {config.account_seed()['total_equity_krw']:,}원")
    except config.RiskLimitError as e:
        # status 는 진단용이다. 한도가 없다고 여기서 죽으면 무엇이 없는지 볼 수 없다.
        # 다만 종료 코드는 실패여야 한다 — 항상 0 을 주는 점검은 아무것도 점검하지 않는다.
        limits_ok = False
        print(f"   리스크 한도: ✗ {e}")
        for item in config.missing_limits():
            print(f"      · {item}")

    print("\n── 보유 포지션 ──")
    rows = conn.execute(
        "SELECT code,name,qty,avg_price,opened_at FROM paper_positions WHERE closed_at IS NULL"
    ).fetchall()
    if not rows:
        print("   없음")
    for c, nm, q, a, o in rows:
        print(f"   {c} {nm or '':<12} {q:>6}주 @{a:>9,}  {o[:10]}")
    return 0 if limits_ok else 3


def task_decide(conn, pack_id: str | None, provider: str | None, model: str | None) -> int:
    """Arm 1·2 를 같은 팩에 대해 짝으로 판단한다 (ADR 0005 3-arm).

    한쪽만 실패하면 성공한 쪽은 쓰되 **쌍에서 제외**된다 — 짝 없는 관측을 쌍으로 세면
    대응비교가 오염된다. 종료 코드 4 가 그 상태다.
    """
    if pack_id:
        row = conn.execute(
            "SELECT payload FROM context_packs WHERE pack_id=?", (pack_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM context_packs ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        log.error("팩이 없다. `python -m decision.pipeline build` 를 먼저 돌려라.")
        return 1

    p = json.loads(row[0])
    try:
        out = engine.decide_pair(conn, p, provider=engine._provider(provider), model=model)
    except engine.DecisionRefused as e:
        log.error("%s", e)
        return 3  # 설정 문제 (대개 ANTHROPIC_API_KEY)

    log.info("제공자 %s · 모델 %s", out["provider"], out["model"])
    for arm in (1, 2):
        r = out.get(f"arm{arm}")
        if r is None:
            log.error("  arm %d — 실패. decisions 테이블에서 시도 기록을 확인하라", arm)
            continue
        payload = json.loads(r["payload"])
        n = len(payload.get("decisions", []))
        log.info(
            "  arm %d — %s · 결정 %d건 · 감시가능 %s/%s · %s토큰",
            arm,
            r["status"],
            n,
            r.get("monitorable"),
            n,
            (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0),
        )
        if r.get("unmonitorable"):
            log.warning("  arm %d — 감시 불가한 invalidation %d건", arm, r["unmonitorable"])

    if not out["paired"]:
        log.warning("한쪽 arm 이 실패해 **대응비교 쌍에서 제외**된다.")
        return 4
    return 0


def task_watch(conn, *, apply: bool) -> int:
    """무효화 감시 (ADR 0013 원칙 2). **기본은 읽기만 한다** — `--apply` 여야 표시한다.

    청산은 하지 않는다. 실행 계층이 0줄이므로 여기서 포지션을 닫으면 킬 스위치도
    멱등성도 없는 자리에서 상태를 바꾸는 것이 된다.
    """

    from decision import invalidation as iv

    day = conn.execute("SELECT MAX(date) FROM ohlcv WHERE volume > 0").fetchone()[0]
    if not day:
        log.error("일봉이 없다 — 먼저 데이터 배치를 돌린다")
        return 1
    verdicts = iv.scan(conn, day)
    if not verdicts:
        log.info("감시 대상 포지션이 없다 (열린 포지션 + invalidation 이 있는 것)")
        return 0

    counts = {s: 0 for s in (iv.HIT, iv.SAFE, iv.UNKNOWN)}
    for v in verdicts:
        counts[v.state] += 1
        mark = {"hit": "깨짐", "safe": "유지", "unknown": "판정불가"}[v.state]
        log.info("  [%s] %s — %s", mark, v.code, v.reason)
    log.info(
        "기준일 %s · 깨짐 %d · 유지 %d · 판정불가 %d",
        day,
        counts[iv.HIT],
        counts[iv.SAFE],
        counts[iv.UNKNOWN],
    )
    # 판정불가가 많으면 감시하는 척만 하는 것이다 — 조용히 넘기지 않는다.
    if counts[iv.UNKNOWN] > counts[iv.HIT] + counts[iv.SAFE]:
        log.warning(
            "판정불가가 절반을 넘는다 — 조건이 감시되지 않고 있다. "
            "invalidation.type 구성이나 데이터 적재를 확인하라"
        )
    if apply and counts[iv.HIT]:
        n = iv.mark_hits(conn, verdicts)
        conn.commit()
        log.info(
            "invalidation_hit 표시 %d건. **청산은 하지 않는다** — 재판단(event 사이클)이 다음이다",
            n,
        )
    elif counts[iv.HIT]:
        log.info("--apply 를 주면 %d건에 invalidation_hit 을 찍는다", counts[iv.HIT])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="decision.pipeline", description="컨텍스트 팩 배치")
    p.add_argument("task", choices=["build", "decide", "show", "status", "watch"])
    p.add_argument("--cycle", choices=list(config.CYCLES), default="premarket")
    p.add_argument("--trigger", default=None, help="event 사이클의 트리거 종류")
    p.add_argument("--code", default=None)
    p.add_argument("--detail", default=None)
    p.add_argument("--pack-id", default=None)
    p.add_argument(
        "--apply", action="store_true", help="watch: 깨진 조건에 invalidation_hit 을 찍는다"
    )
    p.add_argument(
        "--provider",
        choices=providers.available(),
        default=None,
        help="LLM 제공자. 기본값은 AIK_LLM_PROVIDER, 그것도 없으면 anthropic",
    )
    p.add_argument("--model", default=None, help="모델 이름. 기본값은 AIK_LLM_MODEL")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    with store.connect() as conn:
        store.init_db(conn)
        if args.task == "build":
            return task_build(
                conn, cycle=args.cycle, trigger=args.trigger, code=args.code, detail=args.detail
            )
        if args.task == "decide":
            return task_decide(conn, args.pack_id, args.provider, args.model)
        if args.task == "watch":
            return task_watch(conn, apply=args.apply)
        if args.task == "show":
            if not args.pack_id:
                log.error("--pack-id 가 필요하다")
                return 2
            return task_show(conn, args.pack_id)
        return task_status(conn)


if __name__ == "__main__":
    sys.exit(main())
