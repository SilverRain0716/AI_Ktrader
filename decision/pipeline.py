"""컨텍스트 팩 배치.

사용:
    python -m decision.pipeline build --cycle premarket
    python -m decision.pipeline build --cycle event --code 005930 --trigger invalidation_hit
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
from decision import config, pack

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
        p = pack.build(conn, cycle=cycle, event_trigger=event)
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
    print(f"   리스크 한도: {config.constraints()}")

    print("\n── 보유 포지션 ──")
    rows = conn.execute(
        "SELECT code,name,qty,avg_price,opened_at FROM paper_positions WHERE closed_at IS NULL"
    ).fetchall()
    if not rows:
        print("   없음")
    for c, nm, q, a, o in rows:
        print(f"   {c} {nm or '':<12} {q:>6}주 @{a:>9,}  {o[:10]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="decision.pipeline", description="컨텍스트 팩 배치")
    p.add_argument("task", choices=["build", "show", "status"])
    p.add_argument("--cycle", choices=list(config.CYCLES), default="premarket")
    p.add_argument("--trigger", default=None, help="event 사이클의 트리거 종류")
    p.add_argument("--code", default=None)
    p.add_argument("--detail", default=None)
    p.add_argument("--pack-id", default=None)
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
        if args.task == "show":
            if not args.pack_id:
                log.error("--pack-id 가 필요하다")
                return 2
            return task_show(conn, args.pack_id)
        return task_status(conn)


if __name__ == "__main__":
    sys.exit(main())
