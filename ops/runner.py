"""사이클 러너 — 정해진 시각에 한 사이클을 돌린다.

    python -m ops.runner --cycle premarket
    python -m ops.runner --cycle data          # 장 마감 후 배치
    python -m ops.runner --auto                # 지금 시각에 맞는 것

## 스케줄러는 사람이 안 보는 사이에 돈다

그래서 게이트보다 더 방어적이다.

1. **킬 스위치를 먼저 본다.** 게이트도 보지만 그 전에 여기서 멈춘다 —
   판단 호출은 돈이 나가고, 멈추려던 사람은 그것도 멈추길 원한다
2. **중복 실행을 막는다.** cron 이 겹치거나 사람이 동시에 치면 같은 팩이 두 번 만들어진다
3. **실패를 삼키지 않는다.** 종료 코드로 나가고 `ingest_log` 에 남는다

## 주문은 여기서 나가지 않는다

러너는 `build → decide → gate check` 까지다. `place` 는 **부르지 않는다** —
집행은 사람이 확인하고 치는 것이 지금 단계다(하드 규칙 5, `execution/` 0줄).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import date, datetime, time

from data import config as dcfg
from data import store
from gate import config as gcfg
from ops import calendar as cal

log = logging.getLogger("ops")

# 사이클별 실행 시각(KST). ADR 0009 의 판단 사이클 넷 + 데이터 배치.
# **값은 설계안에서 온 것이고 실험이 바꿀 수 있다** — 여기 박아 두되 임시임을 적는다.
SCHEDULE: dict[str, time] = {
    "premarket": time(8, 20),
    "midday": time(12, 20),
    "preclose": time(15, 0),
    "data": time(18, 0),  # 장 마감 후 배치. postmarket 판단보다 먼저 와야 한다
    "postmarket": time(18, 30),
}
JUDGMENT_CYCLES = ("premarket", "midday", "preclose", "postmarket")

LOCK_DIR = dcfg.DATA_DIR / "locks"


def _lock_path(cycle: str, day: date) -> os.PathLike[str]:
    return LOCK_DIR / f"{day.isoformat()}-{cycle}.lock"


def _run(args: list[str]) -> int:
    """하위 명령을 돌린다. **출력을 삼키지 않는다.**"""
    log.info("  $ %s", " ".join(args))
    return subprocess.call([sys.executable, "-m", *args])


def run_cycle(cycle: str, *, day: date | None = None, force: bool = False) -> int:
    """한 사이클. 종료 코드 0=정상 / 2=건너뜀 / 그 외=실패."""
    day = day or datetime.now(dcfg.KST).date()

    kill = gcfg.kill_switch()
    if kill.on:
        log.error("킬 스위치가 켜져 있다 — %s. 아무것도 하지 않는다", kill.reason)
        return 2

    with store.connect() as conn:
        store.init_db(conn)
        ok, why = cal.should_run(conn, day)
    if not ok and not force:
        log.info("건너뜀 — %s", why)
        return 2
    log.info("%s · %s — %s", day, cycle, why)

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(cycle, day)
    if lock.exists() and not force:
        log.error("이미 돌았다 (%s). 다시 돌리려면 --force", lock)
        return 2

    if cycle == "data":
        rc = _run(["data.pipeline", "daily"])
    elif cycle in JUDGMENT_CYCLES:
        rc = _run(["decision.pipeline", "build", "--cycle", cycle])
        if rc == 0:
            rc = _run(["decision.pipeline", "decide"])
        if rc == 0:
            # 게이트는 **판정만** 한다. place 는 부르지 않는다 — 집행은 사람이 확인하고 친다.
            _run(["gate.pipeline", "check", "--latest", "--record"])
            _run(["decision.pipeline", "watch"])
    else:
        log.error("모르는 사이클: %s", cycle)
        return 1

    if rc == 0:
        lock.touch()
    else:
        log.error("%s 실패 (rc=%d) — 락을 남기지 않는다. 고치고 다시 돌릴 수 있다", cycle, rc)
    return rc


def due_now(now: datetime | None = None, *, window_min: int = 20) -> str | None:
    """지금 시각에 해당하는 사이클. cron 이 몇 분 늦어도 잡히게 창을 둔다."""
    now = now or datetime.now(dcfg.KST)
    for cycle, t in SCHEDULE.items():
        planned = datetime.combine(now.date(), t, tzinfo=dcfg.KST)
        delta = (now - planned).total_seconds() / 60
        if 0 <= delta <= window_min:
            return cycle
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ops.runner", description="사이클 러너 (주문 없음)")
    p.add_argument("--cycle", choices=[*SCHEDULE], default=None)
    p.add_argument("--auto", action="store_true", help="지금 시각에 맞는 사이클")
    p.add_argument("--force", action="store_true", help="락·휴장 판정을 무시한다")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    cycle = args.cycle
    if args.auto:
        cycle = due_now()
        if cycle is None:
            log.info("지금 시각에 해당하는 사이클이 없다")
            return 2
    if not cycle:
        log.error("--cycle 또는 --auto 가 필요하다")
        return 1
    return run_cycle(cycle, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
