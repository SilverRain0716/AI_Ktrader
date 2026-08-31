"""주문 시뮬레이터 (2단계).

여기서 지키는 것 넷.

1. **주문일보다 이전 봉으로 체결시키지 않는다.** 결정이 이미 본 봉으로 체결하면
   그 판단의 근거가 곧 체결가가 된다.
2. **`date()` 를 KST 문자열에 쓰지 않는다.** SQLite 가 UTC 로 환산해 새벽이 전날이 된다.
3. **갭 가드는 ATR 배수다** (ADR 0009). 고정 % 로 두면 저변동·고변동 종목에 같은 잣대가 된다.
4. **미체결은 폐기이고 이월하지 않는다.**
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from data import config as dcfg
from data import store
from gate import broker as gb

NOW = datetime(2026, 9, 1, 4, 56, 45, tzinfo=dcfg.KST)  # KST 새벽 — UTC 로는 전날이다
D_DAY = "2026-09-01"


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


def _bar(conn, code, day, o, h, low, c, vol=1_000_000):
    conn.execute(
        "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted)"
        " VALUES (?,?,?,?,?,?,?,0,'t',1)",
        (code, day, o, h, low, c, vol),
    )


def _atr(conn, code, day, pct):
    conn.execute(
        "INSERT OR REPLACE INTO indicators (code,date,payload) VALUES (?,?,?)",
        (code, day, json.dumps({"indicators": {"atr_pct": pct}, "flows": {}})),
    )


def _setup(conn, code="000880", weight=6.0, limit_price=None, created=NOW):
    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES ('D','live',1,'P','s',1,'premarket',?,?,'r1','ok',?)",
        (
            created.isoformat(),
            created.isoformat(),
            json.dumps(
                {"decisions": [{"action": "BUY", "code": code, "name": code, "weight_pct": weight}]}
            ),
        ),
    )
    conn.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,limit_price,mode,"
        "kiwoom_env,created_at,status) VALUES (?, 'D', ?, 'BUY', ?, 'paper','mock',?,'allowed')",
        (f"D-{code}", code, limit_price, created.isoformat()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO listing (code,name,market,updated_at) VALUES (?,?,?,?)",
        (code, code, "KOSPI", "2026-09-01"),
    )


# ── 1. 수량 ─────────────────────────────────────────────


def test_못_사면_0주다():
    """실측: 시드 2천만에서 삼성전기 1주가 145.8만원이라 목표 7% 로는 0주다.

    버그가 아니라 소액 계좌의 현실이고, 페이퍼도 같게 재야 실계좌와 맞는다.
    """
    assert gb.size_for(20_000_000, 7.0, 1_458_000) == 0
    assert gb.size_for(20_000_000, 8.0, 1_458_000) == 1


def test_수량은_내림이다():
    assert gb.size_for(20_000_000, 6.0, 135_500) == 8  # 1,200,000 / 135,500 = 8.85


def test_수량_0이면_접수하지_않는다(db):
    _setup(db, weight=0.1)
    _bar(db, "000880", "2026-08-31", 135_500, 136_000, 135_000, 135_500)
    fills = gb.SimBroker().place(db, "D", now=NOW)
    assert fills[0].status == gb.EXPIRED and "수량 0" in fills[0].reason


# ── 2. 시각 — 여기가 핵심이다 ───────────────────────────


def test_주문일보다_이전_봉으로_체결시키지_않는다(db):
    """**실제로 그랬다**(2026-09-01): 09-01 장을 향한 주문이 08-31 봉으로 체결됐다.

    결정이 이미 본 봉으로 체결하면 판단의 근거가 그대로 체결가가 된다.
    """
    _setup(db)
    _bar(db, "000880", "2026-08-31", 135_500, 136_000, 135_000, 135_500)
    _atr(db, "000880", "2026-08-31", 5.0)
    gb.SimBroker().place(db, "D", now=NOW)
    assert gb.SimBroker().settle(db, "2026-08-31") == [], "과거 봉으로 체결됐다"


def test_KST_새벽이_전날로_밀리지_않는다(db):
    """SQLite `date()` 는 오프셋을 UTC 로 환산한다 —
    '2026-09-01T04:56:45+09:00' → '2026-08-31'. 저장된 문자열이 이미 KST 다.
    """
    got = db.execute(
        "SELECT date(?), substr(?, 1, 10)", (NOW.isoformat(), NOW.isoformat())
    ).fetchone()
    assert got[0] == "2026-08-31", "SQLite 동작이 바뀌었다면 이 테스트의 전제를 다시 본다"
    assert got[1] == "2026-09-01"


def test_주문일_당일_봉이_들어오면_체결된다(db):
    _setup(db)
    _bar(db, "000880", "2026-08-31", 130_000, 136_000, 129_000, 135_500)
    _bar(db, "000880", D_DAY, 136_000, 140_000, 135_000, 138_000)
    _atr(db, "000880", D_DAY, 5.0)
    gb.SimBroker().place(db, "D", now=NOW)
    fills = gb.SimBroker().settle(db, D_DAY)
    assert fills[0].status == gb.FILLED and fills[0].price == 136_000  # MARKET = 시가


# ── 3. 갭 가드 (ADR 0009) ───────────────────────────────


def test_상방_갭이_크면_집행하지_않는다(db, monkeypatch):
    monkeypatch.setenv("AIK_MAX_ENTRY_GAP_UP_ATR", "1.0")
    _setup(db)
    _bar(db, "000880", "2026-08-31", 130_000, 136_000, 129_000, 100_000)
    _bar(db, "000880", D_DAY, 112_000, 115_000, 111_000, 113_000)  # +12%
    _atr(db, "000880", D_DAY, 5.0)  # 12/5 = 2.4 ATR
    gb.SimBroker().place(db, "D", now=NOW)
    f = gb.SimBroker().settle(db, D_DAY)[0]
    assert f.status == gb.GAPPED and "밤새 전제가 깨졌다" in f.reason


def test_갭_판정은_ATR_배수다_고정퍼센트가_아니다(db, monkeypatch):
    """같은 +6% 갭이 ATR 4% 종목에서는 1.5배(차단), 10% 종목에서는 0.6배(통과)다."""
    monkeypatch.setenv("AIK_MAX_ENTRY_GAP_UP_ATR", "1.0")
    for code, atr, expect in (("AAA", 4.0, gb.GAPPED), ("BBB", 10.0, gb.FILLED)):
        _setup(db, code=code)
        db.execute("UPDATE decisions SET decision_id=? WHERE decision_id='D'", (f"D{code}",))
        db.execute("UPDATE order_intents SET decision_id=? WHERE decision_id='D'", (f"D{code}",))
        _bar(db, code, "2026-08-31", 99_000, 101_000, 98_000, 100_000)
        _bar(db, code, D_DAY, 106_000, 107_000, 105_000, 106_500)  # +6%
        _atr(db, code, D_DAY, atr)
        gb.SimBroker().place(db, f"D{code}", now=NOW)
        assert gb.SimBroker().settle(db, D_DAY)[0].status == expect, f"{code} ATR {atr}"


# ── 4. 미체결 ───────────────────────────────────────────


def test_지정가에_못_닿으면_폐기다(db):
    _setup(db, limit_price=120_000)
    _bar(db, "000880", "2026-08-31", 130_000, 136_000, 129_000, 130_000)
    _bar(db, "000880", D_DAY, 131_000, 133_000, 129_000, 132_000)  # 저가 129,000 > 120,000
    _atr(db, "000880", D_DAY, 5.0)
    gb.SimBroker().place(db, "D", now=NOW)
    f = gb.SimBroker().settle(db, D_DAY)[0]
    assert f.status == gb.EXPIRED and "미도달" in f.reason


def test_그날_봉이_없으면_폐기다(db):
    """거래정지·휴장. **이월하지 않는다** (ADR 0009)."""
    _setup(db)
    _bar(db, "000880", "2026-08-31", 130_000, 136_000, 129_000, 135_500)
    gb.SimBroker().place(db, "D", now=NOW)
    assert gb.SimBroker().settle(db, D_DAY)[0].status == gb.EXPIRED


def test_체결된_것만_포지션에_반영한다(db):
    fills = [
        gb.Fill("i1", "000880", gb.FILLED, 8, 135_500),
        gb.Fill("i2", "004370", gb.GAPPED, 2, 0),
        gb.Fill("i3", "005930", gb.EXPIRED, 0, 0),
    ]
    assert gb.apply_fills(db, fills, day=D_DAY) == 1
    rows = list(db.execute("SELECT code, qty, avg_price FROM paper_positions"))
    assert rows == [("000880", 8, 135_500)]


# ── 5. 실제 주문은 없다 ─────────────────────────────────


def test_시뮬레이터는_아무_데도_요청하지_않는다():
    """`execution/` 이 생기기 전까지 여기서 네트워크가 나가면 안 된다."""
    import pathlib

    import gate

    src = (pathlib.Path(gate.__file__).parent / "broker.py").read_text(encoding="utf-8")
    for banned in ("httpx", "requests", "urllib", "KiwoomClient"):
        assert banned not in src, f"시뮬레이터에 네트워크가 들어왔다: {banned}"
