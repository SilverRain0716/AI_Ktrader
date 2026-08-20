"""브리핑 구조화 배치.

사용:
    python -m briefing.pipeline sync                # 새 브리핑만 (이미 있는 건 건너뜀)
    python -m briefing.pipeline sync --full         # 전체 재파싱 (파서를 고쳤을 때)
    python -m briefing.pipeline sync --days 3       # 최근 3일만
    python -m briefing.pipeline map-codes           # 종목명 → 코드 역매핑
    python -m briefing.pipeline status              # 적재 현황과 파싱 경고 요약

운영 시각: 각 브리핑 발행 직후, 또는 하루 한 번 장 마감 후 일괄.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from datetime import datetime

from briefing import config, gitlab, parser
from data import config as dcfg
from data import store

log = logging.getLogger("briefing")


def _now() -> str:
    return datetime.now(dcfg.KST).isoformat(timespec="seconds")


def task_sync(conn, *, days: int | None, full: bool) -> None:
    started = _now()
    try:
        gitlab._token()
    except gitlab.GitLabTokenMissing as e:
        log.warning("브리핑 동기화 건너뜀 — %s", e)
        store.log_ingest(
            conn,
            started_at=started,
            task="briefing_sync",
            target=None,
            status="skip",
            detail=str(e)[:500],
        )
        return

    all_days = gitlab.list_days()
    if days:
        all_days = all_days[-days:]
    existing = set() if full else store.briefing_ids(conn)

    ok = skip = fail = views = 0
    warn_counter: collections.Counter = collections.Counter()

    for day in all_days:
        for stem in gitlab.list_files(day):
            bid = f"{day}-{stem}"
            if bid in existing:
                skip += 1
                continue
            if stem not in config.KINDS:
                fail += 1
                log.error("알 수 없는 브리핑 종류: %s/%s — config.KINDS 에 추가하라", day, stem)
                store.log_ingest(
                    conn,
                    started_at=started,
                    task="briefing_sync",
                    target=bid,
                    status="fail",
                    detail="알 수 없는 stem",
                )
                continue
            try:
                bf = gitlab.fetch(day, stem)
                if bf is None:
                    continue
                url = (
                    f"{config.GITLAB_HOST}/{config.GITLAB_PROJECT}/-/blob/"
                    f"{config.GITLAB_BRANCH}/{config.GITLAB_ROOT}/{day}/{stem}.md"
                )
                p = parser.parse(day, stem, bf.text, source_url=url)
                n = store.upsert_briefing(conn, p.to_dict(), stem=stem, ingested_at=started)
                ok += 1
                views += n
                for w in p.parse_warnings:
                    warn_counter[w.split(":", 1)[-1].strip()[:50]] += 1
                store.log_ingest(
                    conn,
                    started_at=started,
                    task="briefing_sync",
                    target=bid,
                    status="ok",
                    rows=n,
                    detail=f"경고 {len(p.parse_warnings)}건" if p.parse_warnings else None,
                )
            except Exception as e:
                fail += 1
                log.warning("브리핑 실패 %s: %s", bid, e)
                store.log_ingest(
                    conn,
                    started_at=started,
                    task="briefing_sync",
                    target=bid,
                    status="fail",
                    detail=str(e)[:500],
                )

    log.info("브리핑 동기화 — 신규 %d / 건너뜀 %d / 실패 %d / 관점 %d", ok, skip, fail, views)
    if warn_counter:
        log.info("파싱 경고 상위:")
        for msg, n in warn_counter.most_common(5):
            log.info("   %3d회  %s", n, msg)


def task_map_codes(conn) -> None:
    """종목명만 있는 관점에 6자리 코드를 채운다 (kr-preclose 는 원문에 코드가 없다)."""
    started = _now()
    mapping = store.name_to_code_map(conn)
    if not mapping:
        log.error("종목 마스터가 비어 있다. 먼저 `python -m data.pipeline listing` 을 실행하라.")
        return

    rows = conn.execute(
        "SELECT briefing_id, seq, name FROM briefing_views "
        "WHERE market='KR' AND code IS NULL AND name IS NOT NULL"
    ).fetchall()
    hit = miss = 0
    unmatched: collections.Counter = collections.Counter()
    for bid, seq, name in rows:
        code = mapping.get((name or "").replace(" ", ""))
        if code:
            conn.execute(
                "UPDATE briefing_views SET code=? WHERE briefing_id=? AND seq=?", (code, bid, seq)
            )
            hit += 1
        else:
            miss += 1
            unmatched[name] += 1

    store.log_ingest(
        conn,
        started_at=started,
        task="briefing_map_codes",
        target=None,
        status="ok",
        rows=hit,
        detail=f"미매핑 {miss}",
    )
    log.info("코드 매핑 — 성공 %d / 실패 %d", hit, miss)
    if unmatched:
        log.info("미매핑 종목명 상위: %s", unmatched.most_common(10))


def task_status(conn) -> None:
    n_b = conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0]
    n_v = conn.execute("SELECT COUNT(*) FROM briefing_views").fetchone()[0]
    print(f"── 브리핑 {n_b}건 / 관점 {n_v}건 ──")

    rng = conn.execute("SELECT MIN(day), MAX(day) FROM briefings").fetchone()
    if rng and rng[0]:
        print(f"   기간 {rng[0]} ~ {rng[1]}")

    print("\n── 종류별 ──")
    for kind, nb, nv in conn.execute(
        "SELECT kind, COUNT(*), SUM(view_count) FROM briefings GROUP BY kind ORDER BY kind"
    ):
        print(f"   {kind:<22} 파일 {nb:>3}   관점 {nv or 0:>4}")

    print("\n── 관점 분포 ──")
    for stance, n in conn.execute(
        "SELECT stance, COUNT(*) FROM briefing_views GROUP BY stance ORDER BY COUNT(*) DESC"
    ):
        print(f"   {stance:<6} {n:>4}")

    kr = conn.execute("SELECT COUNT(*) FROM briefing_views WHERE market='KR'").fetchone()[0]
    kr_c = conn.execute(
        "SELECT COUNT(*) FROM briefing_views WHERE market='KR' AND code IS NOT NULL"
    ).fetchone()[0]
    print(f"\n   한국 관점 코드 확보 {kr_c}/{kr}")

    print("\n── 파싱 경고 상위 ──")
    c: collections.Counter = collections.Counter()
    for (raw,) in conn.execute("SELECT parse_warnings FROM briefings"):
        for w in json.loads(raw):
            c[w.split(":", 1)[-1].strip()[:50]] += 1
    if not c:
        print("   없음")
    for msg, n in c.most_common(8):
        print(f"   {n:>4}회  {msg}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="briefing.pipeline", description="브리핑 구조화 배치")
    p.add_argument("task", choices=["sync", "map-codes", "status"])
    p.add_argument("--days", type=int, default=None, help="최근 N일만")
    p.add_argument("--full", action="store_true", help="이미 적재된 것도 재파싱")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    with store.connect() as conn:
        store.init_db(conn)
        if args.task == "sync":
            task_sync(conn, days=args.days, full=args.full)
        elif args.task == "map-codes":
            task_map_codes(conn)
        else:
            task_status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
