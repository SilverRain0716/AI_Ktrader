"""집행 게이트 CLI. **주문을 내지 않는다** — 낼 수 있는지만 판정한다.

python -m gate.pipeline status                 # 지금 설정이 안전한가
python -m gate.pipeline check --decision <id>  # 이 결정을 집행해도 되는가
python -m gate.pipeline check --latest --record
"""

from __future__ import annotations

import argparse
import logging
import sys

from data import store
from gate import check as gcheck
from gate import config as gcfg

log = logging.getLogger("gate")


def task_status() -> int:
    kill = gcfg.kill_switch()
    problems = gcfg.check_coherent()
    log.info("집행 모드   %s", gcfg.mode())
    log.info("조회 서버   %s", gcfg.read_base() or "(미설정)")
    log.info("주문 서버   %s  → %s", gcfg.order_base() or "(미설정)", gcfg.order_target())
    log.info(
        "킬 스위치   %s%s",
        "켜짐" if kill.on else "꺼짐",
        f" — {kill.reason}" if kill.reason else "",
    )
    log.info("킬 파일     %s (%s)", gcfg.KILL_FILE, "있음" if gcfg.KILL_FILE.exists() else "없음")
    if problems:
        for p in problems:
            log.error("모순: %s", p)
        return 3
    if gcfg.mode() == gcfg.PAPER:
        log.info("paper 모드다 — 게이트를 통과해도 주문은 나가지 않는다")
    return 0


def task_check(conn, decision_id: str | None, latest: bool, do_record: bool) -> int:
    if latest and not decision_id:
        row = conn.execute(
            "SELECT decision_id FROM decisions WHERE run_kind='live' AND status='ok' "
            "ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            log.error("집행 대상 결정이 없다 (run_kind=live · status=ok)")
            return 1
        decision_id = row[0]
    if not decision_id:
        log.error("--decision 또는 --latest 가 필요하다")
        return 2

    v = gcheck.evaluate(conn, decision_id)
    log.info("결정 %s · 모드 %s", v.decision_id, v.mode)
    for o in v.orders:
        log.info("  주문 후보 %s %s (비중 %s%%)", o["action"], o["code"], o["weight_pct"])
    if v.blockers:
        for b in v.blockers:
            log.warning("  차단: %s", b)
    log.info(
        "판정 %s · 실제 주문이 나가는가: %s",
        "통과" if v.allowed else "차단",
        "예" if v.sends_orders else "아니오",
    )
    if do_record:
        n = gcheck.record(conn, v)
        conn.commit()
        log.info("대장에 %d건 기록 — **주문은 내지 않았다**", n)
    return 0 if v.allowed else 4


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.pipeline", description="집행 게이트 (주문 없음)")
    p.add_argument("task", choices=["status", "check"])
    p.add_argument("--decision", default=None)
    p.add_argument("--latest", action="store_true", help="가장 최근 집행 대상 결정")
    p.add_argument("--record", action="store_true", help="판정을 order_intents 에 남긴다")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s | %(message)s",
    )
    if args.task == "status":
        return task_status()
    with store.connect() as conn:
        store.init_db(conn)
        return task_check(conn, args.decision, args.latest, args.record)


if __name__ == "__main__":
    sys.exit(main())
