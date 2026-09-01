"""판단 통계 — abstain 비율과 **그것이 옳았는지**.

여기서 지키는 것 넷.

1. **프롬프트 버전으로 가른다.** v1 이 산 것과 v4 가 안 산 것을 같은 표본으로 세면
   둘 다 해석 불가가 된다.
2. **마지막 시도만 센다.** 재시도가 여러 행이라 그대로 세면 비율이 왜곡된다.
3. **팩의 유니버스로 사후 성과를 본다.** 지수를 쓰면 "그날 시장"이지
   "그 후보들"이 아니다.
4. **평균이 아니라 중앙값이다.** 한 종목의 급등이 유니버스를 대표하면 안 된다.
"""

from __future__ import annotations

import json

import pytest

from data import store
from decision import stats as S


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


def _decision(
    conn,
    did,
    *,
    arm=1,
    prompt="decision_v4",
    status="abstain",
    attempt=1,
    pack_id="P1",
    run_kind="live",
):
    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,prompt_id,payload) "
        "VALUES (?,?,?,?,'s',?,'midday','2026-09-01T10:00:00+09:00',"
        "'2026-09-01T15:20:00+09:00','r1',?,?,'{}')",
        (did, run_kind, attempt, pack_id, arm, status, prompt),
    )


def _pack(conn, pack_id="P1", *, day="2026-09-01", codes=("AAA", "BBB")):
    conn.execute(
        "INSERT INTO context_packs (pack_id,cycle,generated_at,universe_size,position_count,"
        "view_count,warning_count,payload) VALUES (?,'midday',?,?,0,0,0,?)",
        (
            pack_id,
            day,
            len(codes),
            json.dumps(
                {"data_quality": {"ohlcv_as_of": day}, "universe": [{"code": c} for c in codes]}
            ),
        ),
    )


def _bars(conn, code, closes, start="2026-09-01"):
    from datetime import date, timedelta

    d = date.fromisoformat(start)
    for close in closes:
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,halted,"
            "source,adjusted) VALUES (?,?,?,?,?,?,1000,0,'t',1)",
            (code, d.isoformat(), close, close, close, close),
        )
        d += timedelta(days=1)


# ── 1. 비율 ─────────────────────────────────────────────


def test_프롬프트별로_가른다(db):
    _decision(db, "d1", prompt="decision_v3", status="ok")
    _decision(db, "d2", prompt="decision_v4", status="abstain")
    got = {(r.prompt_id, r.arm): r for r in S.rates(db)}
    assert got[("decision_v3", 1)].rate == 0
    assert got[("decision_v4", 1)].rate == 100


def test_arm별로_가른다(db):
    _decision(db, "d1", arm=1, status="abstain")
    _decision(db, "d2", arm=2, status="ok")
    got = {r.arm: r.rate for r in S.rates(db)}
    assert got == {1: 100.0, 2: 0.0}


def test_재시도는_마지막만_센다(db):
    """재시도가 여러 행이라 그대로 세면 비율이 왜곡된다."""
    _decision(db, "d1", attempt=1, status="ok")
    _decision(db, "d1", attempt=2, status="abstain")
    r = S.rates(db)[0]
    assert (r.total, r.abstain) == (1, 1)


def test_실험_결정은_세지_않는다(db):
    """집행 대상이 아닌 것이 비율에 섞이면 안 된다 (ADR 0014)."""
    _decision(db, "d1", status="abstain", run_kind="experiment")
    assert S.rates(db) == []


@pytest.mark.parametrize("status", ["api_error", "schema_rejected", "contract_rejected"])
def test_장애는_판단이_아니다(db, status):
    """**관측과 결측을 섞으면 F2·F3 표본이 오염된다** (ADR 0007)."""
    _decision(db, "d1", status=status)
    assert S.rates(db) == []


# ── 2. 사후 성과 — 여기가 핵심이다 ──────────────────────


def test_abstain_뒤_유니버스가_오르면_놓친_것이다(db):
    _decision(db, "d1", status="abstain")
    _pack(db)
    _bars(db, "AAA", [100, 101, 102, 103, 104, 110])  # +10%
    _bars(db, "BBB", [100, 101, 102, 103, 104, 110])
    o = S.opportunity(db)[0]
    assert o["r5"] == pytest.approx(10.0)


def test_abstain_뒤_유니버스가_빠지면_피한_것이다(db):
    _decision(db, "d1", status="abstain")
    _pack(db)
    _bars(db, "AAA", [100, 99, 98, 97, 96, 90])
    _bars(db, "BBB", [100, 99, 98, 97, 96, 90])
    assert S.opportunity(db)[0]["r5"] == pytest.approx(-10.0)


def test_중앙값이지_평균이_아니다(db):
    """한 종목의 급등이 유니버스 전체를 대표하면 안 된다."""
    _decision(db, "d1", status="abstain")
    _pack(db, codes=("AAA", "BBB", "CCC"))
    for code, last in (("AAA", 100), ("BBB", 101), ("CCC", 300)):
        _bars(db, code, [100, 100, 100, 100, 100, last])
    o = S.opportunity(db)[0]
    assert o["r5"] == pytest.approx(1.0), "평균이면 +67% 가 나온다"


def test_봉이_모자라면_대기중이다(db):
    """**없는 것을 0 으로 채우지 않는다** — 아직 안 지난 날을 '수익 0' 으로 세면 안 된다."""
    _decision(db, "d1", status="abstain")
    _pack(db)
    _bars(db, "AAA", [100, 101])
    _bars(db, "BBB", [100, 101])
    o = S.opportunity(db)[0]
    assert o["r5"] is None and o["r20"] is None


def test_팩의_유니버스를_쓴다(db):
    """지수를 쓰면 '그날 시장'이지 '그 후보들'이 아니다."""
    _decision(db, "d1", status="abstain")
    _pack(db, codes=("AAA",))
    _bars(db, "AAA", [100, 100, 100, 100, 100, 90])
    _bars(db, "KOSPI", [100, 100, 100, 100, 100, 200])  # 시장은 급등
    assert S.opportunity(db)[0]["r5"] == pytest.approx(-10.0)


def test_ok_인_판단은_사후_성과를_안_본다(db):
    _decision(db, "d1", status="ok")
    _pack(db)
    _bars(db, "AAA", [100] * 6)
    assert S.opportunity(db) == []
