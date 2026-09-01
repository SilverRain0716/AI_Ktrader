"""사이클 러너 — **사람이 안 보는 사이에 도는 것**이라 게이트보다 방어적이어야 한다.

여기서 지키는 것 다섯.

1. **킬 스위치를 먼저 본다.** 게이트도 보지만 그 전에 멈춘다 — 판단 호출은 돈이 나가고,
   멈추려던 사람은 그것도 멈추길 원한다.
2. **주말엔 안 돈다.** 평일 휴장일은 데이터가 없어 뒤에서 막힌다 —
   **틀리는 방향이 안전한 쪽**이다.
3. **중복 실행을 막는다.** cron 이 겹치거나 사람이 동시에 치면 같은 팩이 두 번 만들어진다.
4. **실패하면 락을 남기지 않는다.** 고치고 다시 돌릴 수 있어야 한다.
5. **주문을 내지 않는다.** 러너는 판정까지다.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from data import config as dcfg
from data import store
from ops import calendar as cal
from ops import runner as R


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("KILL_SWITCH", "false")
    from gate import config as gcfg

    monkeypatch.setattr(gcfg, "KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(R, "LOCK_DIR", tmp_path / "locks")


def _index(conn, days):
    for d in days:
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,halted,"
            "source,adjusted) VALUES ('KOSPI',?,1,1,1,1,1,0,'t',1)",
            (d,),
        )


# ── 1. 거래일 판정 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "weekend"),
    [(date(2026, 9, 5), True), (date(2026, 9, 6), True), (date(2026, 9, 7), False)],
)
def test_주말을_가른다(day, weekend):
    assert cal.is_weekend(day) is weekend


def test_평일이면_돈다(db):
    """평일 휴장일은 데이터가 없어 뒤에서 막힌다 — **거래일을 건너뛰는 것보다 낫다.**"""
    _index(db, ["2026-09-01"])
    ok, why = cal.should_run(db, date(2026, 9, 2))
    assert ok and "평일" in why


def test_주말은_안_돈다(db):
    _index(db, ["2026-09-01"])
    ok, _ = cal.should_run(db, date(2026, 9, 5))
    assert not ok


def test_직전_거래일이_너무_멀면_알린다(db):
    """연휴가 7일을 넘는 일은 드물다 — 데이터가 낡았을 가능성이 더 크다."""
    _index(db, ["2026-08-01"])
    ok, why = cal.should_run(db, date(2026, 9, 2))
    assert ok and "결손 의심" in why


def test_지수_봉이_거래일_사실이다(db):
    """음력 명절도 임시공휴일도 대체휴일도 이미 반영돼 있다."""
    _index(db, ["2026-09-01"])
    assert cal.was_trading_day(db, date(2026, 9, 1))
    assert not cal.was_trading_day(db, date(2026, 9, 2))


# ── 2. 킬 스위치 — 가장 먼저 본다 ───────────────────────


def test_킬스위치가_켜지면_아무것도_안_한다(monkeypatch):
    """**게이트보다 먼저다.** 판단 호출은 돈이 나간다."""
    monkeypatch.setenv("KILL_SWITCH", "true")
    called = []
    monkeypatch.setattr(R, "_run", lambda a: called.append(a) or 0)
    assert R.run_cycle("premarket", day=date(2026, 9, 2)) == 2
    assert called == [], "킬 스위치가 켜졌는데 하위 명령이 돌았다"


def test_킬파일로도_멈춘다(monkeypatch, tmp_path):
    from gate import config as gcfg

    gcfg.KILL_FILE.write_text("stop")
    called = []
    monkeypatch.setattr(R, "_run", lambda a: called.append(a) or 0)
    assert R.run_cycle("data", day=date(2026, 9, 2)) == 2
    assert called == []


# ── 3. 중복 실행 ────────────────────────────────────────


def test_같은_사이클을_두_번_돌리지_않는다(monkeypatch):
    """cron 이 겹치거나 사람이 동시에 치면 같은 팩이 두 번 만들어진다."""
    calls = []
    monkeypatch.setattr(R, "_run", lambda a: calls.append(a) or 0)
    day = date(2026, 9, 2)
    assert R.run_cycle("data", day=day) == 0
    n = len(calls)
    assert R.run_cycle("data", day=day) == 2, "두 번째가 통과했다"
    assert len(calls) == n


def test_force_는_락을_무시한다(monkeypatch):
    monkeypatch.setattr(R, "_run", lambda a: 0)
    day = date(2026, 9, 2)
    R.run_cycle("data", day=day)
    assert R.run_cycle("data", day=day, force=True) == 0


def test_실패하면_락을_남기지_않는다(monkeypatch):
    """고치고 다시 돌릴 수 있어야 한다."""
    monkeypatch.setattr(R, "_run", lambda a: 1)
    day = date(2026, 9, 2)
    assert R.run_cycle("data", day=day) == 1
    assert not R._lock_path("data", day).exists()


# ── 4. 시각 판정 ────────────────────────────────────────


@pytest.mark.parametrize(
    ("hhmm", "cycle"),
    [
        ((8, 20), "premarket"),
        ((8, 39), "premarket"),
        ((9, 0), None),
        ((12, 20), "midday"),
        ((15, 5), "preclose"),
        ((18, 0), "data"),
        ((18, 35), "postmarket"),
    ],
)
def test_지금_시각의_사이클(hhmm, cycle):
    now = datetime(2026, 9, 2, *hhmm, tzinfo=dcfg.KST)
    assert R.due_now(now) == cycle


def test_시각_창을_벗어나면_안_잡는다():
    """cron 이 몇 분 늦어도 잡히되, 한참 지난 것을 뒤늦게 돌리지는 않는다."""
    assert R.due_now(datetime(2026, 9, 2, 8, 19, tzinfo=dcfg.KST)) is None
    assert R.due_now(datetime(2026, 9, 2, 8, 41, tzinfo=dcfg.KST)) is None


# ── 5. 주문을 내지 않는다 ───────────────────────────────


def test_러너가_place_를_부르지_않는다(monkeypatch):
    """집행은 사람이 확인하고 친다 (하드 규칙 5, `execution/` 0줄)."""
    calls = []
    monkeypatch.setattr(R, "_run", lambda a: calls.append(a) or 0)
    R.run_cycle("premarket", day=date(2026, 9, 2))
    flat = [" ".join(a) for a in calls]
    assert not any("place" in c for c in flat), f"러너가 주문을 접수했다: {flat}"
    assert any("check" in c for c in flat), "게이트 판정은 해야 한다"


# ── 6. 하루 보고서 ──────────────────────────────────────


def _dec(
    conn,
    did,
    *,
    cycle="premarket",
    arm=1,
    status="ok",
    at="2026-09-02T08:25:00+09:00",
    buys=(),
    attempt=1,
):
    import json as _j

    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,prompt_id,payload) "
        "VALUES (?,'live',?,'P','s',?,?,?,?,'r1',?,'decision_v4',?)",
        (
            did,
            attempt,
            arm,
            cycle,
            at,
            at,
            status,
            _j.dumps(
                {
                    "decisions": [
                        {"action": "BUY", "code": c, "name": c, "weight_pct": 5} for c in buys
                    ]
                }
            ),
        ),
    )


def _bar(conn, code, day):
    conn.execute(
        "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,halted,source,"
        "adjusted) VALUES (?,?,1,1,1,1,100,0,'t',1)",
        (code, day),
    )


def test_보고서가_사이클_결손을_먼저_잡는다(db, caplog):
    """**자동으로 돌면 안 돈 것도 조용하다.** 실제로 09-01 은 preclose·postmarket 을
    아예 안 돌렸는데 아무도 몰랐다.
    """
    import logging

    from ops import report as rep

    _dec(db, "d1", cycle="premarket")
    _bar(db, "AAA", "2026-09-02")
    with caplog.at_level(logging.INFO):
        rc = rep.report(db, date(2026, 9, 2))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert rc == 1
    assert "결손" in msgs and "preclose" in msgs and "postmarket" in msgs


def test_사이클이_다_돌면_조용하다(db, caplog):
    """정상은 조용히, 이상은 크게 — 매일 같은 분량이 쏟아지면 읽지 않게 된다."""
    import logging

    from ops import report as rep

    for i, c in enumerate(R.JUDGMENT_CYCLES):
        _dec(db, f"d{i}", cycle=c)
    _bar(db, "AAA", "2026-09-02")
    with caplog.at_level(logging.INFO):
        rc = rep.report(db, date(2026, 9, 2))
    assert rc == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_일봉이_없으면_알린다(db, caplog):
    import logging

    from ops import report as rep

    for i, c in enumerate(R.JUDGMENT_CYCLES):
        _dec(db, f"d{i}", cycle=c)
    with caplog.at_level(logging.INFO):
        assert rep.report(db, date(2026, 9, 2)) == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "일봉이 없다" in msgs


def test_재시도는_마지막만_보인다(db, caplog):
    import logging

    from ops import report as rep

    _dec(db, "d1", attempt=1, status="schema_rejected")
    _dec(db, "d1", attempt=2, status="ok", buys=("AAA",))
    _bar(db, "AAA", "2026-09-02")
    with caplog.at_level(logging.INFO):
        rep.report(db, date(2026, 9, 2))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "schema_rejected" not in msgs


def test_차단된_주문은_경고로_올린다(db, caplog):
    import logging

    from ops import report as rep

    for i, c in enumerate(R.JUDGMENT_CYCLES):
        _dec(db, f"d{i}", cycle=c)
    _bar(db, "AAA", "2026-09-02")
    db.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,reason,arm) VALUES ('i1','d0','AAA','BUY','paper','mock',"
        "'2026-09-02T09:00:00+09:00','blocked','킬 스위치',1)"
    )
    with caplog.at_level(logging.INFO):
        assert rep.report(db, date(2026, 9, 2)) == 1
    assert any(r.levelno >= logging.WARNING and "blocked" in r.getMessage() for r in caplog.records)
