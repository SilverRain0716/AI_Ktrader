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

    v = gcheck.evaluate(conn, decision_id, deposit_krw=_deposit_if_needed(gcfg.arm_of(decision_id)))
    log.info("결정 %s · 모드 %s", v.decision_id, v.mode)
    for o in v.orders:
        log.info("  주문 후보 %s %s (비중 %s%%)", o["action"], o["code"], o["weight_pct"])
    for n in v.notes:
        log.info("  알림: %s", n)
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


def _client_for(arm: int):
    """그 arm 의 계좌에 붙는 클라이언트. **사람이 계좌를 지정하지 않는다**(ADR 0014)."""
    import os

    from data.sources.kiwoom import KiwoomClient

    ak, sk = gcfg.credentials_for(arm)
    os.environ["KIWOOM_APP_KEY"], os.environ["KIWOOM_APP_SECRET"] = ak, sk
    return KiwoomClient(base=gcfg.order_base())


def _deposit_if_needed(arm: int) -> int | None:
    """`mock`/`live` 에서만 계좌를 조회한다. **paper 는 계좌를 건드리지 않는다.**

    실패해도 예외를 밖으로 내지 않는다 — `None` 이 곧 "확인 못 했다"이고,
    게이트가 그것을 차단 사유로 쓴다(잔고 부족과 확인 실패를 섞지 않기 위해서다).
    """
    if gcfg.mode() not in (gcfg.MOCK, gcfg.LIVE):
        return None
    from gate import account as gacct

    try:
        return gacct.deposit_krw(_client_for(arm))
    except Exception as e:
        log.error("예수금 조회 실패 — %s: %s", type(e).__name__, e)
        return None


def _latest_live(conn) -> str | None:
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE run_kind='live' AND status='ok' "
        "ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def task_place(conn, decision_id: str | None, latest: bool) -> int:
    """게이트를 통과한 의도에 수량을 채워 접수한다. **체결은 아직 아니다.**

    **접수 전에 이전 사이클의 미체결을 폐기한다** (ADR 0009 결정 3, 이월 금지).
    `abstain` 결정에도 폐기는 돈다 — 그것도 판단이고, 옛 주문은 그 상황에서
    나온 것이 아니다.
    """
    from datetime import datetime

    from data import config as dcfg
    from gate import broker as gb

    decision_id = decision_id or (_latest_live(conn) if latest else None)
    if not decision_id:
        log.error("--decision 또는 --latest 가 필요하다")
        return 2

    # **판정보다 먼저 한다.** 게이트가 막든 말든 옛 판단은 이미 무효다.
    for f in gb.supersede(conn, decision_id):
        log.warning("  폐기 %s — %s", f.code, f.reason)
    conn.commit()

    st = conn.execute(
        "SELECT status FROM decisions WHERE decision_id=? ORDER BY attempt DESC LIMIT 1",
        (decision_id,),
    ).fetchone()
    if st and st[0] == "abstain":
        log.info("%s 은 abstain 이다 — 신규 접수 없음. 옛 미체결만 폐기했다", decision_id)
        return 0

    v = gcheck.evaluate(conn, decision_id, deposit_krw=_deposit_if_needed(gcfg.arm_of(decision_id)))
    for n in v.notes:
        log.info("알림: %s", n)
    if not v.allowed:
        for b in v.blockers:
            log.error("차단: %s", b)
        return 4
    gcheck.record(conn, v)

    b = gb.SimBroker()
    log.info("브로커 %s · 모드 %s — **실제 주문은 나가지 않는다**", b.name, gcfg.mode())
    for f in b.place(conn, decision_id, now=datetime.now(dcfg.KST)):
        if f.status == gb.SENT:
            log.info("  접수 %s %d주 @기준 %s", f.code, f.qty, f"{f.price:,}")
        else:
            log.warning("  %s %s — %s", f.status, f.code, f.reason)
    conn.commit()
    return 0


def task_settle(conn, day: str | None) -> int:
    """접수분을 그날 일봉으로 판정한다. **미체결은 폐기이고 이월하지 않는다** (ADR 0009)."""
    from gate import broker as gb

    day = day or conn.execute("SELECT MAX(date) FROM ohlcv WHERE volume>0").fetchone()[0]
    if not day:
        log.error("일봉이 없다")
        return 1
    # 접수는 됐는데 그날 봉이 아직 없는 경우를 조용히 넘기지 않는다.
    pending = conn.execute(
        "SELECT COUNT(*) FROM order_intents WHERE status='sent' AND substr(created_at, 1, 10) > ?",
        (day,),
    ).fetchone()[0]
    if pending:
        log.warning(
            "접수 %d건이 %s 이후를 향한다 — 그날 일봉이 들어와야 체결을 판정할 수 있다",
            pending,
            day,
        )
    b = gb.SimBroker()
    fills = b.settle(conn, day)
    if not fills:
        log.info("접수 상태인 주문이 없다")
        return 0
    for f in fills:
        if f.status == gb.FILLED:
            log.info("  체결 %s %d주 @%s", f.code, f.qty, f"{f.price:,}")
        else:
            log.warning("  %s %s — %s", f.status, f.code, f.reason)
    n = gb.apply_fills(conn, fills, day=day)
    conn.commit()
    log.info(
        "기준일 %s · 체결 %d · 미체결 %d · 포지션 반영 %d",
        day,
        sum(1 for f in fills if f.status == gb.FILLED),
        sum(1 for f in fills if f.status != gb.FILLED),
        n,
    )
    return 0


def task_fills(conn, *, apply: bool) -> int:
    """증권사에 체결을 물어 대장을 맞춘다. **추정하지 않는다.**

    `paper` 에서는 부를 이유가 없다 — 주문이 나가지 않았으므로 계좌에 아무것도 없다.
    """
    from data.sources.kiwoom import KiwoomClient
    from gate import fills as gf

    if gcfg.mode() == gcfg.PAPER:
        log.info("paper 모드다 — 증권사에 낸 주문이 없다")
        return 0
    problems = gcfg.check_coherent()
    if problems:
        for x in problems:
            log.error("모순: %s", x)
        return 3

    client = KiwoomClient(base=gcfg.order_base())
    execs, bad = gf.fetch(client)
    for b in bad:
        log.error("  %s", b)
    log.info("체결 내역 %d건 · 읽지 못한 행 %d건", len(execs), len(bad))

    r = gf.reconcile(conn, execs) if apply else None
    if r is None:
        for e in execs:
            log.info("  체결 %s %d주 @%s (주문 %s)", e.code, e.qty, f"{e.price:,}", e.order_no)
        log.info("--apply 를 주면 대장을 갱신한다")
        return 0
    conn.commit()
    log.info("맞춰진 것 %d · 미체결 %d", len(r["matched"]), len(r["pending"]))
    if r["unknown"]:
        for e in r["unknown"]:
            log.error(
                "  **대장에 없는 체결** %s %d주 @%s (주문 %s) — 사람이 냈거나 중복이다",
                e.code,
                e.qty,
                f"{e.price:,}",
                e.order_no,
            )
    if r["unreferenced"]:
        log.error(
            "  주문번호 없는 sent %d건 — 어댑터가 주문번호를 못 받았다. 대조가 불가능하다",
            r["unreferenced"],
        )
    return 4 if (r["unknown"] or r["unreferenced"] or bad) else 0


def task_positions(conn) -> int:
    """계좌의 실제 보유와 우리 기록을 **양방향**으로 맞춘다. **아무것도 고치지 않는다.**

    자동으로 맞추면 어느 쪽이 틀렸는지 모른 채 덮인다.
    """
    from gate import fills as gf

    if gcfg.mode() == gcfg.PAPER:
        log.info("paper 모드다 — 증권사 계좌에 보유가 없다")
        return 0
    problems = gcfg.check_coherent()
    if problems:
        for x in problems:
            log.error("모순: %s", x)
        return 3

    bad_total = 0
    names = dict(conn.execute("SELECT code,name FROM listing"))
    for arm in sorted(gcfg.ARM_KEY_ENV):
        held, bad = gf.holdings(_client_for(arm))
        for b in bad:
            log.error("  %s", b)
        r = gf.reconcile_positions(conn, held, arm=arm)
        log.info("arm%d · 계좌 %d종목 · 일치 %d", arm, len(held), r["agreed"])
        for h in r["only_broker"]:
            log.error(
                "   **우리가 모르는 보유** %s %d주 @%s — 체결 확인을 놓쳤거나 사람이 직접 샀다",
                names.get(h.code, h.code),
                h.qty,
                f"{h.avg_price:,}",
            )
        for code, _pid, qty, avg in r["only_ours"]:
            log.error(
                "   **계좌에 없는 보유** %s %d주 @%s — 청산을 놓쳤다. 무효화 감시가 유령을 본다",
                names.get(code, code),
                qty,
                f"{avg:,}",
            )
        for code, oq, oa, tq, ta in r["mismatch"]:
            log.error(
                "   **수량·평단 불일치** %s 우리 %d주@%s vs 계좌 %d주@%s — 계좌가 맞다",
                names.get(code, code),
                oq,
                f"{oa:,}",
                tq,
                f"{ta:,}",
            )
        bad_total += len(r["only_broker"]) + len(r["only_ours"]) + len(r["mismatch"]) + len(bad)
    if bad_total:
        log.error(
            "어긋남 %d건 — **자동으로 고치지 않는다.** 어느 쪽이 틀렸는지 사람이 본다", bad_total
        )
    return 4 if bad_total else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate.pipeline", description="집행 게이트 (주문 없음)")
    p.add_argument("task", choices=["status", "check", "place", "settle", "fills", "positions"])
    p.add_argument("--decision", default=None)
    p.add_argument("--latest", action="store_true", help="가장 최근 집행 대상 결정")
    p.add_argument("--record", action="store_true", help="판정을 order_intents 에 남긴다")
    p.add_argument("--day", default=None, help="settle: 체결을 판정할 거래일 (기본 최신 일봉)")
    p.add_argument(
        "--apply",
        action="store_true",
        help="fills: 조회한 체결로 대장을 갱신한다. 주지 않으면 읽기만 한다",
    )
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
        if args.task == "check":
            return task_check(conn, args.decision, args.latest, args.record)
        if args.task == "place":
            return task_place(conn, args.decision, args.latest)
        if args.task == "settle":
            return task_settle(conn, args.day)
        if args.task == "fills":
            return task_fills(conn, apply=args.apply)
        return task_positions(conn)


if __name__ == "__main__":
    sys.exit(main())
