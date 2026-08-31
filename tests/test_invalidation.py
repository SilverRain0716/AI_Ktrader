"""무효화 감시 (ADR 0013 원칙 2).

여기서 지키는 것 넷.

1. **수급이 주 조건이다.** 원칙 2 가 *"% 가 아니라 수급과 재료 소멸로 판단한다"* 이다.
2. **판정 못 하면 UNKNOWN 이다.** SAFE 로 접으면 빈 테이블 조회가 조용히 통과한
   악재공시 필터와 같은 사고가 된다.
3. **미래를 보지 않는다.** `on` 이후 봉·수급·공시는 판정에 들어가면 안 된다.
4. **청산하지 않는다.** 표시만 남긴다 — 실행 계층은 아직 0줄이다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from data import store
from decision import invalidation as iv

ON = "2026-08-28"


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


def _bars(conn, code, closes, values_eok=None, end=ON):
    """closes 는 과거→현재 순. 거래대금(억)은 values_eok 로 준다."""
    d = date.fromisoformat(end) - timedelta(days=len(closes) - 1)
    vals = values_eok or [100.0] * len(closes)
    for close, val in zip(closes, vals, strict=True):
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted)"
            " VALUES (?,?,?,?,?,?,?,0,'t',1)",
            (code, d.isoformat(), close, close, close, close, int(val * 1e8 / close)),
        )
        d += timedelta(days=1)


def _flows(conn, code, pairs, end=ON):
    """pairs 는 과거→현재 순의 (외국인, 기관) 순매수 수량."""
    d = date.fromisoformat(end) - timedelta(days=len(pairs) - 1)
    for f, i in pairs:
        conn.execute(
            "INSERT OR REPLACE INTO flows (code,date,inst_net_qty,foreign_net_qty,source)"
            " VALUES (?,?,?,?,'t')",
            (code, d.isoformat(), i, f),
        )
        d += timedelta(days=1)


# ── 1. 수급 이탈 — 원칙 2 의 본체 ───────────────────────


def test_외국인_기관_동반_순매도가_기준일수를_넘으면_깨진다(db):
    _flows(db, "A", [(100, 100), (-1, -1), (-1, -1), (-1, -1)])
    r = iv.evaluate(db, {"type": "flow_reversal", "value": 3}, "A", ON)
    assert r.state == iv.HIT and r.observed == 3


def test_한쪽만_팔면_깨지지_않는다(db):
    """한쪽만 파는 것은 흔하다. 한쪽 기준으로 두면 거의 매일 참이 된다."""
    _flows(db, "A", [(-1, 100), (-1, 100), (-1, 100), (-1, 100)])
    assert iv.evaluate(db, {"type": "flow_reversal", "value": 3}, "A", ON).state == iv.SAFE


def test_수급_데이터가_없으면_UNKNOWN_이다(db):
    """SAFE 로 접으면 '수급이 멀쩡하다'고 거짓말하게 된다."""
    r = iv.evaluate(db, {"type": "flow_reversal", "value": 3}, "A", ON)
    assert r.state == iv.UNKNOWN


def test_수급_판정은_미래를_보지_않는다(db):
    _flows(db, "A", [(100, 100), (100, 100)], end=ON)
    _flows(db, "A", [(-1, -1), (-1, -1), (-1, -1)], end="2026-09-05")
    assert iv.evaluate(db, {"type": "flow_reversal", "value": 1}, "A", ON).state == iv.SAFE


# ── 2. 관심 소멸 ────────────────────────────────────────


def test_거래대금이_평균_아래로_식으면_깨진다(db):
    _bars(db, "A", [1000.0] * 21, values_eok=[100.0] * 20 + [40.0])
    r = iv.evaluate(db, {"type": "volume_dryup", "value": 0.6}, "A", ON)
    assert r.state == iv.HIT and r.observed == pytest.approx(0.4, abs=0.01)


def test_거래대금이_살아_있으면_안_깨진다(db):
    _bars(db, "A", [1000.0] * 21, values_eok=[100.0] * 20 + [90.0])
    assert iv.evaluate(db, {"type": "volume_dryup", "value": 0.6}, "A", ON).state == iv.SAFE


def test_봉이_모자라면_UNKNOWN_이다(db):
    _bars(db, "A", [1000.0] * 5)
    assert iv.evaluate(db, {"type": "volume_dryup", "value": 0.6}, "A", ON).state == iv.UNKNOWN


# ── 3. 나머지 타입 ──────────────────────────────────────


def test_이동평균_하회(db):
    _bars(db, "A", [1000.0] * 19 + [800.0])
    assert iv.evaluate(db, {"type": "close_below_ma", "value": 20}, "A", ON).state == iv.HIT


def test_공시_유형_발생(db):
    db.execute(
        "INSERT INTO disclosures (rcept_no,rcept_dt,corp_code,code,report_nm,category,material,url)"
        " VALUES ('r1','20260827','c','A','유상증자결정','증자',1,'')"
    )
    assert (
        iv.evaluate(db, {"type": "disclosure_category", "value": "증자"}, "A", ON).state == iv.HIT
    )


def test_미래_공시는_보지_않는다(db):
    db.execute(
        "INSERT INTO disclosures (rcept_no,rcept_dt,corp_code,code,report_nm,category,material,url)"
        " VALUES ('r1','20260905','c','A','유상증자결정','증자',1,'')"
    )
    assert (
        iv.evaluate(db, {"type": "disclosure_category", "value": "증자"}, "A", ON).state == iv.SAFE
    )


def test_unstructured_는_감시되지_않는다(db):
    assert iv.evaluate(db, {"type": "unstructured", "value": None}, "A", ON).state == iv.UNKNOWN


def test_관점_반전은_판정기가_없다고_말한다(db):
    """없는 것을 있는 척하지 않는다. SAFE 로 두면 감시되는 줄 알게 된다."""
    r = iv.evaluate(db, {"type": "stance_reversal", "value": None}, "A", ON)
    assert r.state == iv.UNKNOWN and "판정기" in r.reason


def test_기한이_지나면_감시를_멈춘다(db):
    _flows(db, "A", [(-1, -1)] * 5)
    inv = {"type": "flow_reversal", "value": 1, "deadline": "2026-08-20"}
    assert iv.evaluate(db, inv, "A", ON).state == iv.SAFE


# ── 4. 스캔과 표시 ──────────────────────────────────────


def _pos(conn, pid, code, inv, closed=None):
    conn.execute(
        "INSERT INTO paper_positions (position_id,code,name,qty,avg_price,opened_at,closed_at,"
        "invalidation,invalidation_hit) VALUES (?,?,?,1,1000,'2026-08-01',?,?,0)",
        (pid, code, code, closed, json.dumps(inv)),
    )


def test_열린_포지션만_판정한다(db):
    _flows(db, "A", [(-1, -1)] * 4)
    _flows(db, "B", [(-1, -1)] * 4)
    _pos(db, "p1", "A", {"type": "flow_reversal", "value": 3})
    _pos(db, "p2", "B", {"type": "flow_reversal", "value": 3}, closed="2026-08-10")
    got = iv.scan(db, ON)
    assert [x.position_id for x in got] == ["p1"]


def test_깨진_것만_표시하고_청산하지_않는다(db):
    """청산은 실행 계층의 일이고 그것은 아직 0줄이다. 킬 스위치도 멱등성도 없다."""
    _flows(db, "A", [(-1, -1)] * 4)
    _flows(db, "B", [(100, 100)] * 4)
    _pos(db, "p1", "A", {"type": "flow_reversal", "value": 3})
    _pos(db, "p2", "B", {"type": "flow_reversal", "value": 3})
    n = iv.mark_hits(db, iv.scan(db, ON))
    assert n == 1
    rows = dict(db.execute("SELECT position_id, invalidation_hit FROM paper_positions"))
    assert rows == {"p1": 1, "p2": 0}
    still_open = db.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE closed_at IS NULL"
    ).fetchone()[0]
    assert still_open == 2, "감시기가 포지션을 닫았다"


def test_깨진_JSON_은_UNKNOWN_으로_남는다(db):
    db.execute(
        "INSERT INTO paper_positions (position_id,code,name,qty,avg_price,opened_at,"
        "invalidation,invalidation_hit) VALUES ('p1','A','A',1,1000,'2026-08-01','{{bad',0)"
    )
    got = iv.scan(db, ON)
    assert got[0].state == iv.UNKNOWN
