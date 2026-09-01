"""체결 확인 — **증권사에 물어서 채운다. 추정하지 않는다.**

`SimBroker.settle()` 은 일봉으로 추정한다. 시뮬레이터에서는 그것이 최선이다.
**실주문은 조회다** — 부분체결·거부·정정이 실제로 일어나고 평균단가를 우리가 계산할 수 없다.

여기서 지키는 것 넷.

1. **필드를 못 읽으면 버리지 않고 사유로 남긴다.** 잘못 읽은 값으로 대장을 채우면
   "체결됐다고 믿는데 아닌" 상태가 된다.
2. **대조는 양방향이다.** 대장에 없는 체결 = 우리가 내지 않은 주문이다.
3. **주문번호 없이는 대조하지 않는다.** 종목·수량으로 맞추면 중복 주문에서 틀린다.
4. **`sent` 만 갱신한다.** 이미 끝난 것을 다시 건드리지 않는다.
"""

from __future__ import annotations

import pytest

from data import store
from gate import fills as gf


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


class _Fake:
    def __init__(self, rows, key="cntr"):
        self.rows, self.key = rows, key

    def post(self, tr, path, body):
        return {"return_code": 0, self.key: self.rows}


def _intent(conn, intent_id, code, *, status="sent", ref=None):
    conn.execute(
        "INSERT INTO order_intents (intent_id,decision_id,code,action,mode,kiwoom_env,"
        "created_at,status,broker_ref) VALUES (?,'D',?,'BUY','mock','mock','2026-09-01',?,?)",
        (intent_id, code, status, ref),
    )


# ── 1. 파싱 ─────────────────────────────────────────────


def test_체결을_읽는다():
    rows = [{"ord_no": "A1", "stk_cd": "005930", "cntr_qty": "10", "cntr_uv": "+70000"}]
    got, bad = gf.fetch(_Fake(rows))
    assert not bad
    assert got == [gf.Execution("A1", "005930", 10, 70000)]


def test_부호_접두사를_뗀다():
    """키움의 `-257000` 은 음수가 아니라 하락 표시다 (데이터 계층 함정)."""
    rows = [{"ord_no": "A1", "stk_cd": "005930", "cntr_qty": "1", "cntr_uv": "-257000"}]
    assert gf.fetch(_Fake(rows))[0][0].price == 257000


def test_읽지_못한_행을_버리지_않는다():
    """모의계좌에 주문이 없어 실물 응답을 못 봤다 — 필드명이 어긋나면 드러나야 한다."""
    rows = [{"모르는키": "x", "another": 1}]
    got, bad = gf.fetch(_Fake(rows))
    assert got == []
    assert bad and "읽을 수 없다" in bad[0] and "another" in bad[0]


def test_수량이_0이어도_체결이다():
    """0 과 '없음'은 다르다. 취소·부분체결에서 0 이 올 수 있다."""
    rows = [{"ord_no": "A1", "stk_cd": "005930", "cntr_qty": "0", "cntr_uv": "70000"}]
    got, bad = gf.fetch(_Fake(rows))
    assert not bad and got[0].qty == 0


# ── 2. 대조 — 여기가 핵심이다 ───────────────────────────


def test_주문번호로_맞춘다(db):
    _intent(db, "i1", "005930", ref="A1")
    r = gf.reconcile(db, [gf.Execution("A1", "005930", 10, 70000)])
    assert len(r["matched"]) == 1
    row = db.execute("SELECT status, qty, fill_price FROM order_intents").fetchone()
    assert row == ("filled", 10, 70000)


def test_대장에_없는_체결을_드러낸다(db):
    """**사람이 HTS 로 냈거나 중복 주문이다.** 조용히 넘기면 계좌 상태를 영영 못 맞춘다."""
    r = gf.reconcile(db, [gf.Execution("ZZ", "005930", 10, 70000)])
    assert len(r["unknown"]) == 1 and not r["matched"]


def test_주문번호는_맞는데_종목이_다르면_불명이다(db):
    """대조가 깨진 것이다. 맞춰 버리면 엉뚱한 종목을 체결로 기록한다."""
    _intent(db, "i1", "005930", ref="A1")
    r = gf.reconcile(db, [gf.Execution("A1", "000660", 10, 70000)])
    assert len(r["unknown"]) == 1 and not r["matched"]
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == "sent"


def test_체결_내역에_없는_sent_는_미체결이다(db):
    _intent(db, "i1", "005930", ref="A1")
    r = gf.reconcile(db, [])
    assert r["pending"] == ["A1"] and not r["matched"]


def test_주문번호_없는_sent_를_드러낸다(db):
    """어댑터가 주문번호를 못 받았다는 뜻이다 — **대조 자체가 불가능하다.**

    실측(2026-09-01): SimBroker 가 넣은 접수분 2건이 그 상태였다.
    """
    _intent(db, "i1", "005930", ref=None)
    assert gf.reconcile(db, [])["unreferenced"] == 1


@pytest.mark.parametrize("status", ["allowed", "filled", "gapped", "expired", "blocked"])
def test_sent_가_아니면_건드리지_않는다(db, status):
    _intent(db, "i1", "005930", status=status, ref="A1")
    gf.reconcile(db, [gf.Execution("A1", "005930", 10, 70000)])
    assert db.execute("SELECT status FROM order_intents").fetchone()[0] == status


# ── 3. 주문 코드가 없다 ─────────────────────────────────


def test_체결_조회에_주문_코드가_없다():
    """읽기 전용이어야 `execution/` 없이 지금 만들 수 있다 (하드 규칙 1·5)."""
    import pathlib

    import gate

    src = (pathlib.Path(gate.__file__).parent / "fills.py").read_text(encoding="utf-8")
    for banned in ("kt10000", "kt10001", "ord_qty", "place_order"):
        assert banned not in src, f"체결 조회에 주문 코드가 들어왔다: {banned}"
