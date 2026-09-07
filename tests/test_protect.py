"""기계 안전망 — **판단을 기다리지 않고 나간다.**

`invalidation` 은 판단이고 감시기는 표시만 한다(ADR 0013 원칙 2).
여기 둘은 봉투다 — AI 의견을 묻지 않는다.

이것이 없으면 **AI 가 매 사이클 빠짐없이 봐주는 것에 전부 걸린다.** 실제로
2026-09-07 에 arm 2 가 손절선의 3/4 까지 온 2 종목을 통째로 빠뜨렸고,
`stop_price` 를 읽는 코드는 저장하는 쪽 말고 한 줄도 없었다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from data import config as dcfg
from data import store
from decision import positions as P
from gate import protect as pr

NOW = datetime(2026, 9, 7, 16, 0, tzinfo=dcfg.KST)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    return conn


def _bar(conn, code, day, o, h, low, c):
    conn.execute(
        "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted) "
        "VALUES (?,?,?,?,?,?,1000,0,'t',1)",
        (code, day, o, h, low, c),
    )


def _pos(conn, code="005930", *, arm=1, stop=None, max_days=None, opened="2026-09-01"):
    P.open_position(
        conn=conn,
        position_id=f"p-{code}-a{arm}",
        arm=arm,
        code=code,
        name=code,
        qty=10,
        avg_price=70000,
        opened_at=opened,
        stop_price=stop,
        max_hold_days=max_days,
    )


# ── 손절선 ──────────────────────────────────────────────


def test_저가가_손절선에_닿으면_잡는다(db):
    """**저가로 판정한다.** 종가만 보면 장중에 크게 뚫고 되돌아온 날을 "안 닿았다"고
    읽는다 — 실계좌에서는 이미 체결됐을 자리다.
    """
    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)  # 저가만 뚫었다
    (b,) = pr.scan(db, "2026-09-04")
    assert b.kind == pr.STOP
    assert "저가 67,500" in b.reason


def test_손절선_위면_잡지_않는다(db):
    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 68100, 69800)
    assert pr.scan(db, "2026-09-04") == []


def test_손절선이_없으면_판정하지_않는다(db):
    """ATR 을 몰라 손절선을 못 세운 포지션이 있다 — **없는 것을 0 으로 보지 않는다.**"""
    _pos(db, stop=None)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 1, 2)
    assert pr.scan(db, "2026-09-04") == []


# ── 보유기한 ────────────────────────────────────────────


def test_보유기한을_넘기면_잡는다(db):
    _pos(db, max_days=3, opened="2026-09-01")
    for i, d in enumerate(["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]):
        _bar(db, "005930", d, 70000, 70500, 69500, 70000 + i)
    (b,) = pr.scan(db, "2026-09-07")
    assert b.kind == pr.EXPIRY
    assert "4거래일" in b.reason


def test_보유기한은_거래일로_센다(db):
    """달력일로 세면 **주말이 보유기간에 들어간다.**"""
    _pos(db, max_days=3, opened="2026-09-01")
    _bar(db, "005930", "2026-09-02", 70000, 70500, 69500, 70000)
    _bar(db, "005930", "2026-09-07", 70000, 70500, 69500, 70000)  # 달력으로는 6일
    assert pr.scan(db, "2026-09-07") == []  # 거래일로는 2일


def test_손절이_기한보다_먼저다(db):
    """둘 다 걸리면 손절이 사유다 — 왜 나갔는지가 달라진다."""
    _pos(db, stop=68000, max_days=1)
    _bar(db, "005930", "2026-09-02", 70000, 70500, 69500, 70000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67000, 69000)
    (b,) = pr.scan(db, "2026-09-04")
    assert b.kind == pr.STOP


# ── 결정 행 ─────────────────────────────────────────────


def test_강제_청산은_run_kind_로_갈린다(db):
    """**판단 통계가 오염되면 안 된다** — abstain 비율·F2·F3 는 live 만 센다."""
    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)
    assert pr.enforce(db, pr.scan(db, "2026-09-04"), day="2026-09-04", now=NOW) == 1

    r = db.execute("SELECT run_kind, status, arm, payload FROM decisions").fetchone()
    assert r[0] == pr.RUN_KIND
    assert r[1] == "ok"
    d = json.loads(r[3])["decisions"]
    assert [(x["action"], x["code"]) for x in d] == [("EXIT", "005930")]


def test_arm_마다_따로_남긴다(db):
    """섞으면 게이트가 어느 계좌의 주문인지 모른다."""
    for arm in (1, 2):
        _pos(db, arm=arm, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)
    assert pr.enforce(db, pr.scan(db, "2026-09-04"), day="2026-09-04", now=NOW) == 2
    got = sorted(r[0] for r in db.execute("SELECT decision_id FROM decisions"))
    assert got == ["2026-09-04-protect-a1", "2026-09-04-protect-a2"]


def test_결정_id_에서_arm_을_읽을_수_있다():
    """게이트가 여기서 arm 을 읽는다 — 형식이 어긋나면 집행이 막힌다."""
    from gate import config as gcfg

    assert gcfg.arm_of(pr.decision_id("2026-09-04", 2)) == 2


def test_두_번_돌려도_한_번만_남는다(db):
    """멱등하지 않으면 사이클마다 같은 청산이 쌓인다."""
    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)
    br = pr.scan(db, "2026-09-04")
    pr.enforce(db, br, day="2026-09-04", now=NOW)
    assert pr.enforce(db, br, day="2026-09-04", now=NOW) == 0
    assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_그날_안에_만료된다(db):
    """이월하면 안전망이 아니라 **밀린 주문**이다."""
    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)
    pr.enforce(db, pr.scan(db, "2026-09-04"), day="2026-09-04", now=NOW)
    assert db.execute("SELECT valid_until FROM decisions").fetchone()[0].startswith("2026-09-04")


def test_게이트가_protect_를_집행_대상으로_본다(db, monkeypatch, tmp_path):
    """`run_kind != 'live'` 로 막으면 **안전망이 통째로 차단된다.**"""
    from gate import check as gcheck
    from gate import config as gcfg

    monkeypatch.setenv("KILL_SWITCH", "false")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setattr(gcfg, "KILL_FILE", tmp_path / "KILL")

    _pos(db, stop=68000)
    _bar(db, "005930", "2026-09-04", 70000, 70500, 67500, 69800)
    pr.enforce(db, pr.scan(db, "2026-09-04"), day="2026-09-04", now=NOW)

    did = pr.decision_id("2026-09-04", 1)
    v = gcheck.evaluate(db, did, now=datetime(2026, 9, 4, 15, 0, tzinfo=dcfg.KST))
    assert v.allowed, v.blockers
    assert [o["action"] for o in v.orders] == ["EXIT"]
