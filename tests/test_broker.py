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
from decision import config as ccfg
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
    # arm 은 **주문 대장이 정본**이라 대장에 행이 있어야 반영된다
    _decision_row(db, "D2", "2026-08-31T09:00:00+09:00")
    for iid, code in (("i1", "000880"), ("i2", "004370"), ("i3", "005930")):
        db.execute(
            "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
            "created_at,status,arm) VALUES (?,'D2',?,'BUY','paper','mock',"
            "'2026-08-31T09:00:00+09:00','sent',1)",
            (iid, code),
        )
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


# ── 6. 이월 금지 (ADR 0009 결정 3) ──────────────────────


def _decision_row(conn, did, at, status="ok", valid=None):
    """`valid` 를 주지 않으면 `at` 이 곧 만료다 — 대부분의 테스트는 만료를 안 본다.

    `task_place` 처럼 **실제 시계로** 만료를 재는 경로에서는 미래 시각을 준다.
    고정 시각을 쓰면 그 테스트는 언젠가 조용히 만료로 실패한다.
    """
    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES (?,'live',1,'P','s',1,'premarket',?,?,'r1',?,'{}')",
        (did, at, valid or at, status),
    )


def _sent(conn, intent_id, did, code):
    conn.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status) VALUES (?,?,?,'BUY','paper','mock','2026-09-01','sent')",
        (intent_id, did, code),
    )


def test_새_판단이_나오면_옛_미체결을_폐기한다(db):
    """**실제로 그 구멍이 있었다**(2026-09-01).

    v3 가 접수한 2건이 남아 있는데 v4 가 양쪽 arm 모두 abstain 했다.
    그대로 두면 최신 판단과 어긋난 주문이 체결된다.
    """
    _decision_row(db, "OLD", "2026-09-01T10:35:00+09:00")
    _decision_row(db, "NEW", "2026-09-01T10:56:00+09:00", status="abstain")
    _sent(db, "i1", "OLD", "009150")

    out = gb.supersede(db, "NEW")
    assert [f.status for f in out] == [gb.SUPERSEDED]
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == gb.SUPERSEDED


def test_자기_주문은_건드리지_않는다(db):
    """재시도가 자기 주문을 지우면 접수가 사라진다."""
    _decision_row(db, "D", "2026-09-01T10:35:00+09:00")
    _sent(db, "i1", "D", "009150")
    assert gb.supersede(db, "D") == []
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == "sent"


def test_더_나중_결정의_주문은_건드리지_않는다(db):
    """옛 결정으로 place 를 다시 부른다고 최신 접수를 지우면 안 된다."""
    _decision_row(db, "OLD", "2026-09-01T10:35:00+09:00")
    _decision_row(db, "NEW", "2026-09-01T10:56:00+09:00")
    _sent(db, "i1", "NEW", "009150")
    assert gb.supersede(db, "OLD") == []
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == "sent"


# `allowed` 는 여기 없다. **끝난 것이 아니라 아직 시작 안 한 것**이다 —
# 처음에 같은 목록에 넣었더니 접수 전 주문이 사흘째 살아 있었다 (2026-09-04).
@pytest.mark.parametrize("status", ["filled", "expired", "gapped"])
def test_이미_끝난_것은_폐기하지_않는다(db, status):
    _decision_row(db, "OLD", "2026-09-01T10:35:00+09:00")
    _decision_row(db, "NEW", "2026-09-01T10:56:00+09:00")
    _sent(db, "i1", "OLD", "009150")
    db.execute("UPDATE order_intents SET status=?", (status,))
    assert gb.supersede(db, "NEW") == []


def test_abstain_결정으로_place_를_불러도_옛_주문이_폐기된다(db, monkeypatch):
    """**abstain 은 "사지 않는다"는 판단이다** — 옛 주문은 그 상황에서 나온 것이 아니다.

    소스 순서가 아니라 **동작**으로 검사한다. abstain 에서 일찍 빠져나가면
    폐기가 돌지 않는데, 그것이 정확히 실측된 구멍이었다(2026-09-01).
    """
    from gate import pipeline as gp

    _decision_row(db, "OLD", "2026-09-01T10:35:00+09:00")
    _decision_row(
        db,
        "NEW-a1",
        "2026-09-01T10:56:00+09:00",
        status="abstain",
        valid="2099-01-01T00:00:00+09:00",
    )
    _sent(db, "i1", "OLD", "009150")

    rc = gp.task_place(db, "NEW-a1", latest=False)
    assert rc == 0
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == gb.SUPERSEDED


def test_접수_전_주문도_이월되지_않는다(db):
    """`sent` 만 폐기하면 **판정만 되고 접수 안 된 것이 영원히 남는다** —
    2026-09-02 의 `allowed` 4건이 이틀 뒤에도 그대로였다.
    나중에 누가 place 를 치면 사흘 전 판단으로 주문이 나간다.
    """
    _decision_row(db, "OLD", "2026-09-02T08:40:00+09:00")
    _decision_row(db, "NEW", "2026-09-04T08:24:00+09:00", status="abstain")
    for iid, code, status in (
        ("i-allowed", "005930", "allowed"),
        ("i-sent", "000660", "sent"),
        ("i-filled", "035720", "filled"),
    ):
        db.execute(
            "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
            "created_at,status) VALUES (?,'OLD',?,'BUY','paper','mock',"
            "'2026-09-02T08:41:00+09:00',?)",
            (iid, code, status),
        )

    out = gb.supersede(db, "NEW")
    got = dict(db.execute("SELECT intent_id,status FROM order_intents").fetchall())
    assert got["i-allowed"] == gb.SUPERSEDED
    assert got["i-sent"] == gb.SUPERSEDED
    # 이미 체결된 것은 되돌릴 수 없다 — 폐기 대상이 아니다
    assert got["i-filled"] == "filled"
    assert len(out) == 2


def test_체결은_주문을_낸_arm_계좌로_간다(db, monkeypatch):
    """**분리해 놓고 마지막 단계에서 합쳐버리면 F2·F3 를 잴 수 없다.**

    `settle` 은 여러 arm 의 미체결을 한 번에 처리하므로 arm 을 인자로 고를 수 없다.
    실제로 기본값 1 이 그대로 쓰여 arm 2 의 체결이 arm 1 계좌에 들어갔다
    (2026-09-04, 우리금융지주·한화생명).
    """
    _decision_row(db, "D-a2", "2026-09-04T08:27:00+09:00")
    for iid, code, arm in (("i1", "316140", 2), ("i2", "005930", 1)):
        db.execute(
            "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
            "created_at,status,arm) VALUES (?,'D-a2',?,'BUY','paper','mock',"
            "'2026-09-04T08:27:00+09:00','sent',?)",
            (iid, code, arm),
        )
    fills = [
        gb.Fill("i1", "316140", gb.FILLED, qty=86, price=34650),
        gb.Fill("i2", "005930", gb.FILLED, qty=10, price=70000),
    ]
    assert gb.apply_fills(db, fills, day="2026-09-04") == 2

    got = dict(db.execute("SELECT code,arm FROM paper_positions").fetchall())
    assert got == {"316140": 2, "005930": 1}


def test_대장에_없는_체결은_아무_계좌에도_넣지_않는다(db):
    """어느 계좌 것인지 모르는 체결을 1번에 밀어넣으면 **그 계좌의 수익률이 조용히 오염된다.**"""
    fills = [gb.Fill("없는주문", "316140", gb.FILLED, qty=10, price=1000)]
    assert gb.apply_fills(db, fills, day="2026-09-04") == 0
    assert db.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0


# ── 진입 조건을 포지션에 싣는다 ────────────────────────


def _decision_with(db, did, at, decisions, *, pack_id="P", pack_payload=None):
    import json as _j

    if pack_payload is not None:
        db.execute(
            "INSERT OR IGNORE INTO context_packs (pack_id,cycle,generated_at,universe_size,"
            "position_count,view_count,warning_count,payload) VALUES (?,'premarket',?,1,0,0,0,?)",
            (pack_id, at, _j.dumps(pack_payload)),
        )
    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES (?,'live',1,?,'s',2,'premarket',?,?,'r1','ok',?)",
        (did, pack_id, at, at, _j.dumps({"decisions": decisions})),
    )
    db.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,arm) VALUES ('i9',?,?,'BUY','paper','mock',?,'sent',2)",
        (did, decisions[0]["code"], at),
    )


_INV = {"type": "flow_reversal", "value": 2, "deadline": None, "text": None}


def test_진입_근거와_무효화_조건이_포지션에_남는다(db):
    """없으면 **무효화 감시가 포지션을 통째로 건너뛴다**(`WHERE invalidation IS NOT NULL`).
    손절선도 보유기한도 없이 무기한 방치된다 — 2026-09-07 에 실제로 그 상태였다.
    """
    import json as _j

    _decision_with(
        db,
        "D-a2",
        "2026-09-04T08:27:00+09:00",
        [
            {
                "action": "BUY",
                "code": "316140",
                "name": "우리금융지주",
                "weight_pct": 15,
                "rank": 1,
                "reasons": ["거래대금 2.03배", "외국인 3일 순매수"],
                "invalidation": _INV,
                "stop": {"type": "ATR", "value": 1.5},
                "max_hold_days": 20,
            }
        ],
        pack_payload={"universe": [{"code": "316140", "indicators": {"atr14": 1000}}]},
    )
    gb.apply_fills(db, [gb.Fill("i9", "316140", gb.FILLED, 86, 34650)], day="2026-09-04")

    r = db.execute(
        "SELECT entry_decision_id,entry_thesis,invalidation,stop_price,max_hold_days "
        "FROM paper_positions WHERE code='316140'"
    ).fetchone()
    assert r[0] == "D-a2"
    assert "거래대금 2.03배" in r[1]
    assert _j.loads(r[2]) == _INV
    # 1.5 ATR = 1,500 을 **체결가**에서 뺀다 (결정 시점 종가가 아니다)
    assert r[3] == 34650 - 1500
    assert r[4] == 20


def test_ATR_을_모르면_손절선을_만들어내지_않는다(db):
    """0 이나 체결가를 넣으면 손절선이 있는 것처럼 보이면서 **즉시 걸리거나 영원히 안 걸린다.**"""
    _decision_with(
        db,
        "D2-a2",
        "2026-09-04T08:27:00+09:00",
        [
            {
                "action": "BUY",
                "code": "316140",
                "name": "우리금융지주",
                "weight_pct": 15,
                "rank": 1,
                "reasons": ["근거"],
                "invalidation": _INV,
                "stop": {"type": "ATR", "value": 1.5},
                "max_hold_days": 20,
            }
        ],
        pack_payload={"universe": []},  # ATR 없음
    )
    gb.apply_fills(db, [gb.Fill("i9", "316140", gb.FILLED, 86, 34650)], day="2026-09-04")
    got = db.execute("SELECT stop_price,invalidation FROM paper_positions").fetchone()
    assert got[0] is None
    assert got[1] is not None, "손절선을 못 세워도 무효화 조건은 남아야 한다"


def test_손절은_결정_시점이_아니라_체결가에서_잰다(db):
    """의도한 배수가 되려면 **실제로 들어간 가격**이 기준이어야 한다."""
    terms = gb.entry_terms.__doc__
    assert "체결가에서" in terms


def test_결정을_못_찾아도_포지션은_연다(db):
    """진입 조건이 없다고 체결을 버리면 **계좌와 어긋난다.** 조건만 NULL 로 둔다."""
    db.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,arm) VALUES ('i9','없는결정','316140','BUY','paper','mock',"
        "'2026-09-04T08:27:00+09:00','sent',2)"
    )
    assert (
        gb.apply_fills(db, [gb.Fill("i9", "316140", gb.FILLED, 86, 34650)], day="2026-09-04") == 1
    )
    got = db.execute("SELECT qty,invalidation FROM paper_positions").fetchone()
    assert got == (86, None)


# ── 매도 집행 ───────────────────────────────────────────


def _pos(db, code, qty, avg, *, arm=2, pid=None):
    from decision import positions as P

    P.open_position(
        conn=db,
        position_id=pid or f"p-{code}",
        arm=arm,
        code=code,
        name=code,
        qty=qty,
        avg_price=avg,
        opened_at="2026-09-04",
    )


def _sell_intent(db, iid, did, code, action, weight, *, arm=2):
    db.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,arm) VALUES (?,?,?,?,'paper','mock','2026-09-07T10:00:00+09:00',"
        "'allowed',?)",
        (iid, did, code, action, arm),
    )


def _sell_decision(db, did, decisions):
    import json as _j

    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES (?,'live',1,'P','s',2,'midday','2026-09-07T10:00:00+09:00',"
        "'2026-09-07T15:20:00+09:00','r1','abstain',?)",
        (did, _j.dumps({"decisions": decisions})),
    )


def test_EXIT_은_보유_전량이다(db):
    """**목표비중으로 재면 EXIT 은 weight_pct 가 null 이라 항상 수량 0 이 된다** —
    AI 가 아무리 나가라고 해도 주문이 안 나갔다 (2026-09-07).
    """
    _pos(db, "088350", 327, 6110)
    _sell_decision(db, "S-a2", [{"action": "EXIT", "code": "088350", "weight_pct": None}])
    _sell_intent(db, "s1", "S-a2", "088350", "EXIT", None)
    _bar(db, "088350", "2026-08-31", 6100, 6150, 6050, 6110)

    (f,) = gb.SimBroker().place(db, "S-a2", now=NOW)
    assert (f.status, f.qty) == (gb.SENT, 327)


def test_TRIM_은_줄이는_만큼이다(db):
    """`weight_pct` 는 **축소 후 남길 비중**이다. 그만큼 '사는' 수량을 내면 방향이 반대다."""
    equity = ccfg.account_seed()["total_equity_krw"]
    keep = gb.size_for(equity, 7, 33200)  # 축소 후 남길 수량
    held = keep * 2  # 목표의 두 배를 들고 있다 = 절반을 판다
    _pos(db, "316140", held, 34650)
    _sell_decision(db, "T-a2", [{"action": "TRIM", "code": "316140", "weight_pct": 7}])
    _sell_intent(db, "t1", "T-a2", "316140", "TRIM", 7)
    _bar(db, "316140", "2026-08-31", 33100, 33400, 33000, 33200)

    (f,) = gb.SimBroker().place(db, "T-a2", now=NOW)
    assert f.qty == held - keep
    assert 0 < f.qty < held


def test_보유가_없으면_팔_것이_없다(db):
    _sell_decision(db, "N-a2", [{"action": "EXIT", "code": "088350", "weight_pct": None}])
    _sell_intent(db, "n1", "N-a2", "088350", "EXIT", None)
    (f,) = gb.SimBroker().place(db, "N-a2", now=NOW)
    assert f.status == gb.EXPIRED and "보유가 없다" in f.reason


def test_매도에는_갭_가드를_걸지_않는다(db):
    """**갭 하락한 날이 나가야 할 날이다.** 진입 가드를 매도에 걸면 못 나온다 —
    환경변수 이름부터 `MAX_ENTRY_GAP_*` 이다.
    """
    _pos(db, "088350", 327, 6110)
    db.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,qty,arm) VALUES ('s2','S2','088350','EXIT','paper','mock',"
        "'2026-09-06T10:00:00+09:00','sent',327,2)"
    )
    _bar(db, "088350", "2026-09-04", 6100, 6150, 6050, 6110)
    _bar(db, "088350", "2026-09-07", 5000, 5100, 4900, 5000)  # -18% 갭
    # **ATR 이 없으면 갭 가드 자체가 안 돌아 되돌림 시험이 무의미해진다.**
    db.execute(
        "INSERT INTO indicators (code,date,payload) VALUES ('088350','2026-09-04',?)",
        (json.dumps({"indicators": {"atr_pct": 2.0}}),),
    )
    # 매수였다면 -18% / 2.0 = -9 ATR 로 확실히 걸린다
    assert abs((5000 - 6110) / 6110 * 100 / 2.0) > 1

    fills = gb.SimBroker().settle(db, "2026-09-07")
    assert [f.status for f in fills] == [gb.FILLED]
    assert fills[0].price == 5000


def test_매도_지정가는_고가로_판정한다(db):
    """사는 지정가는 저가가 내려와야 붙고, **파는 지정가는 고가가 올라와야** 붙는다."""
    _pos(db, "088350", 100, 6000)
    for iid, did, limit in (("m1", "S3a", 5900), ("m2", "S3b", 6300)):
        db.execute(
            "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
            "created_at,status,qty,limit_price,arm) VALUES (?,?,'088350','EXIT','paper',"
            "'mock','2026-09-06T10:00:00+09:00','sent',100,?,2)",
            (iid, did, limit),
        )
    _bar(db, "088350", "2026-09-04", 6100, 6150, 6050, 6110)
    _bar(db, "088350", "2026-09-07", 5950, 6100, 5800, 6000)

    got = {f.intent_id: f.status for f in gb.SimBroker().settle(db, "2026-09-07")}
    assert got["m1"] == gb.FILLED  # 고가 6,100 ≥ 5,900
    assert got["m2"] == gb.EXPIRED  # 고가 6,100 < 6,300


def test_EXIT_체결은_포지션을_닫는다(db):
    _pos(db, "088350", 327, 6110)
    _sell_intent(db, "x1", "X", "088350", "EXIT", None)
    db.execute("UPDATE order_intents SET status='filled' WHERE intent_id='x1'")
    assert (
        gb.apply_fills(db, [gb.Fill("x1", "088350", gb.FILLED, 327, 5740)], day="2026-09-07") == 1
    )

    r = db.execute(
        "SELECT closed_at, exit_price, realized_pnl_krw FROM paper_positions WHERE code='088350'"
    ).fetchone()
    assert r[0] == "2026-09-07"
    assert r[1] == 5740
    assert r[2] < 0  # 6,110 → 5,740


def test_TRIM_체결은_수량만_줄인다(db):
    _pos(db, "316140", 86, 34650)
    _sell_intent(db, "x2", "X", "316140", "TRIM", 7)
    assert (
        gb.apply_fills(db, [gb.Fill("x2", "316140", gb.FILLED, 46, 33550)], day="2026-09-07") == 1
    )

    r = db.execute(
        "SELECT qty, closed_at, realized_pnl_krw FROM paper_positions WHERE code='316140'"
    ).fetchone()
    assert r[0] == 40  # 86 - 46
    assert r[1] is None, "일부만 팔았는데 포지션이 닫혔다"
    assert r[2] < 0


def test_보유_없는_매도_체결은_반영하지_않는다(db):
    """**없는 포지션을 만들어내지 않는다.** 대장과 계좌가 어긋난 것이고 드러나야 한다."""
    _sell_intent(db, "x3", "X", "088350", "EXIT", None)
    assert gb.apply_fills(db, [gb.Fill("x3", "088350", gb.FILLED, 10, 5000)], day="2026-09-07") == 0
    assert db.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0


def test_전량만큼_축소하면_전량_청산과_같다(db):
    """두 경로가 손익을 각자 계산하면 같은 거래가 다른 숫자를 낸다."""
    from decision import positions as P

    _pos(db, "A", 10, 1000, pid="pa")
    _pos(db, "B", 10, 1000, pid="pb")
    P.close_position(db, "pa", closed_at="2026-09-07", exit_price=900, exit_reason="EXIT")
    P.reduce_position(db, "pb", qty=10, at="2026-09-07", exit_price=900, exit_reason="TRIM")
    got = [r[0] for r in db.execute("SELECT realized_pnl_krw FROM paper_positions ORDER BY code")]
    assert got[0] == got[1]


def test_두_번_줄이면_손익이_누적된다(db):
    """덮어쓰면 **첫 번째 축소가 없던 일이 된다.**"""
    from decision import positions as P

    _pos(db, "A", 10, 1000, pid="pa")
    P.reduce_position(db, "pa", qty=3, at="2026-09-07", exit_price=900, exit_reason="TRIM")
    first = db.execute("SELECT realized_pnl_krw FROM paper_positions").fetchone()[0]
    P.reduce_position(db, "pa", qty=3, at="2026-09-08", exit_price=900, exit_reason="TRIM")
    second = db.execute("SELECT realized_pnl_krw FROM paper_positions").fetchone()[0]
    assert second < first < 0
    assert second == pytest.approx(first * 2, rel=0.01)
