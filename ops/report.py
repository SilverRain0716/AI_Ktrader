"""하루 보고서 — **아침에 한 번 보면 어제 무슨 일이 있었는지 알 수 있어야 한다.**

    python -m ops.report            # 마지막 거래일
    python -m ops.report --day 2026-09-01

## 자동으로 돌면 사람이 안 보게 된다

스케줄러가 생기면서 사이클이 조용히 돈다. 그러면 **안 돈 것도 조용하다** —
실제로 09-01 은 preclose·postmarket 을 아예 안 돌렸는데 아무도 몰랐다.
그래서 이 보고서는 **일어난 일보다 안 일어난 일**을 먼저 본다.

## 정상은 조용히, 이상은 크게

매일 같은 분량이 쏟아지면 읽지 않게 되고, 그러면 이상이 묻힌다.
결손·차단·경고만 `WARNING` 이상으로 올린다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date

from data import store
from decision import config as ccfg
from decision import positions as P
from ops.runner import JUDGMENT_CYCLES

log = logging.getLogger("ops.report")


def _decisions(conn: sqlite3.Connection, day: str) -> list[dict]:
    """그날의 **최종** 결정들. 재시도는 마지막만."""
    sql = """
        SELECT decision_id, cycle, arm, status, prompt_id, payload, generated_at
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY decision_id ORDER BY attempt DESC) rn
            FROM decisions WHERE run_kind='live' AND substr(generated_at,1,10)=?
        ) WHERE rn=1 ORDER BY generated_at, arm
    """
    out = []
    for did, cyc, arm, st, pid, payload, gen in conn.execute(sql, (day,)):
        buys, holds = [], []
        if payload:
            p = json.loads(payload)
            for d in p.get("decisions") or []:
                if d["action"] in ("BUY", "ADD"):
                    buys.append(f"{d['name']}({d.get('weight_pct')}%)")
                elif d["action"] == "HOLD":
                    holds.append(d["name"])
        out.append(
            {
                "id": did,
                "cycle": cyc,
                "arm": arm,
                "status": st,
                "prompt": pid,
                "at": gen[11:16],
                "buys": buys,
                "holds": holds,
            }
        )
    return out


def report(conn: sqlite3.Connection, day: date) -> int:
    """`0` 정상 · `1` 볼 것이 있다."""
    d = day.isoformat()
    problems = 0
    log.info("═══ %s 보고서 ═══", d)

    # ── 1. 안 일어난 일부터 본다
    decs = _decisions(conn, d)
    ran = {x["cycle"] for x in decs}
    missing = [c for c in JUDGMENT_CYCLES if c not in ran]
    if missing:
        log.warning("판단 사이클 결손: %s", ", ".join(missing))
        problems += 1
    else:
        log.info("판단 사이클 4/4 완료")

    bars = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE date=? AND volume>0", (d,)).fetchone()[0]
    if bars == 0:
        log.warning("그날 일봉이 없다 — 휴장이었거나 배치가 안 돌았다")
        problems += 1
    else:
        log.info("일봉 %d종목", bars)

    # ── 2. 무슨 판단이 나왔나
    if not decs:
        log.warning("판단 기록이 없다")
        return 1
    log.info("── 판단")
    for x in decs:
        mark = (
            "abstain"
            if x["status"] == "abstain"
            else ("ok" if x["status"] == "ok" else x["status"])
        )
        line = ", ".join(x["buys"]) or "-"
        fn = log.info if x["status"] in ("ok", "abstain") else log.warning
        fn(
            "   %s %-10s arm%d %-8s %-13s %s",
            x["at"],
            x["cycle"],
            x["arm"],
            mark,
            x["prompt"] or "?",
            line,
        )
        if x["status"] not in ("ok", "abstain"):
            problems += 1

    # ── 3. 주문은 어떻게 됐나
    rows = list(
        conn.execute(
            "SELECT code, arm, qty, status, substr(COALESCE(reason,''),1,44) FROM order_intents "
            "WHERE substr(created_at,1,10)=? ORDER BY created_at",
            (d,),
        )
    )
    log.info("── 주문 (%d건)", len(rows))
    names = dict(conn.execute("SELECT code,name FROM listing"))
    for code, arm, qty, st, why in rows:
        fn = log.warning if st in ("blocked", "gapped", "failed", "rejected") else log.info
        fn("   %-12s arm%d %3s주 %-11s %s", names.get(code, code)[:11], arm, qty or "-", st, why)
        if st in ("blocked", "failed", "rejected"):
            problems += 1
    if not rows:
        log.info("   없음")

    # ── 4. 계좌 (arm 별)
    log.info("── 계좌")
    seed = ccfg.account_seed()["total_equity_krw"]
    for arm in (1, 2):
        a = P.account_state(conn, seed, arm)
        pos = P.load_open(conn, day, a["total_equity_krw"], arm)
        log.info(
            "   arm%d  총자산 %11s · 현금 %11s · 보유 %d종목%s",
            arm,
            f"{a['total_equity_krw']:,}",
            f"{a['cash_available_krw']:,}",
            len(pos),
            (" — " + ", ".join(p["code"] for p in pos)) if pos else "",
        )

    # ── 5. 무효화 감시
    from decision import invalidation as iv

    v = iv.scan(conn, d)
    if v:
        hit = [x for x in v if x.hit]
        unk = [x for x in v if x.state == iv.UNKNOWN]
        (log.warning if hit else log.info)(
            "── 무효화: 깨짐 %d · 유지 %d · 판정불가 %d",
            len(hit),
            len(v) - len(hit) - len(unk),
            len(unk),
        )
        for x in hit:
            log.warning("   깨짐 %s — %s", x.code, x.reason)
        problems += len(hit)

    # ── 6. 비용
    from ops import cost as cst

    for u in cst.total(conn, since=d):
        usd = u.usd()
        hit = u.cache_hit_pct
        log.info(
            "── 비용 %s · 호출 %d · 입력 %s · 출력 %s · %s",
            u.model,
            u.calls,
            f"{u.input_tokens:,}",
            f"{u.output_tokens:,}",
            f"${usd:.3f}" if usd is not None else "단가 모름 — 금액 없음",
        )
        if hit is None:
            # **0 이 아니라 모른다.** 재지 못한 것을 "캐시 안 먹음"으로 보고하면
            # 없는 개선 여지를 만들어낸다.
            log.warning("   캐시 적중률을 재지 못했다 — 제공자가 값을 주지 않았다")
        else:
            log.info("   캐시 적중 %.1f%% (%s 토큰)", hit, f"{u.cached_tokens:,}")

    log.info("═══ %s ═══", "볼 것 없음" if problems == 0 else f"확인할 것 {problems}건")
    return 0 if problems == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ops.report", description="하루 보고서")
    p.add_argument("--day", default=None, help="YYYY-MM-DD (기본: 마지막 거래일)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
    with store.connect() as conn:
        store.init_db(conn)
        if args.day:
            day = date.fromisoformat(args.day)
        else:
            from ops import calendar as cal

            day = cal.last_trading_day(conn) or date.today()
        return report(conn, day)


if __name__ == "__main__":
    sys.exit(main())
