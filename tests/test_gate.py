"""집행 게이트 (주문 없음).

여기서 지키는 것 넷.

1. **킬 스위치가 최우선이다.** 그리고 **읽을 수 없으면 켜진 것으로 본다** —
   이 실수는 되돌릴 수 없다.
2. **파일로도 켜진다.** 환경변수는 프로세스 시작 시 고정돼, 정작 멈추고 싶은
   순간에 돌고 있는 배치를 못 멈춘다.
3. **모드와 서버가 어긋나면 막는다.** 실측(2026-09-01): `.env` 가
   `EXECUTION_MODE` 가 `paper` 인데 `KIWOOM_ENV` 는 `real` 이었다 — 모의투자를 켜는 순간
   실전 서버로 주문이 갈 상태였다.
4. **실험 결정은 절대 집행하지 않는다.**
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from data import config as dcfg
from data import store
from gate import check as gcheck
from gate import config as gcfg

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=dcfg.KST)


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """게이트 환경을 테스트가 통제한다. 기본은 '가장 안전한 통과 상태'다."""
    monkeypatch.setenv("KILL_SWITCH", "false")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("KIWOOM_ORDER_BASE", "https://mockapi.kiwoom.com")
    monkeypatch.delenv("KIWOOM_ENV", raising=False)
    monkeypatch.delenv("AIK_LIVE_ACK", raising=False)
    monkeypatch.setattr(gcfg, "KILL_FILE", tmp_path / "KILL")


def _decision(conn, did="D1", *, run_kind="live", status="ok", valid=None, codes=("005930",)):
    payload = json.dumps(
        {
            "decisions": [
                {"action": "BUY", "code": c, "name": c, "weight_pct": 5.0, "entry": {"price": 100}}
                for c in codes
            ]
        },
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES (?,?,1,'P','s',1,'premarket',?,?,'r1',?,?)",
        (
            did,
            run_kind,
            NOW.isoformat(),
            valid or (NOW + timedelta(hours=5)).isoformat(),
            status,
            payload,
        ),
    )
    return did


# ── 1. 킬 스위치 ────────────────────────────────────────


def test_킬스위치_환경변수가_막는다(db, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "true")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    assert not v.allowed and any("킬 스위치" in b for b in v.blockers)


def test_킬파일이_막는다(db):
    """환경변수는 프로세스 시작 시 고정된다 — 돌고 있는 배치를 못 멈춘다."""
    gcfg.KILL_FILE.write_text("stop")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    assert not v.allowed and any("킬 파일" in b for b in v.blockers)


@pytest.mark.parametrize("raw", ["maybe", "2", "켜짐", "TRUE-ish"])
def test_해석할_수_없으면_켜진_것으로_본다(db, monkeypatch, raw):
    """'꺼짐'으로 접으면 킬 스위치가 조용히 무력화된다. 되돌릴 수 없는 실수다."""
    monkeypatch.setenv("KILL_SWITCH", raw)
    assert gcfg.kill_switch().on
    assert not gcheck.evaluate(db, _decision(db), now=NOW).allowed


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
def test_명시적으로_꺼진_값만_통과한다(monkeypatch, raw):
    monkeypatch.setenv("KILL_SWITCH", raw)
    assert not gcfg.kill_switch().on


# ── 2. 모드와 서버 — 여기가 핵심이다 ────────────────────


def test_모의라면서_실전서버면_막는다(db, monkeypatch):
    """실측: .env 의 EXECUTION_MODE 는 paper 인데 KIWOOM_ENV 는 real 이었다.

    조회만 하던 동안은 무해했지만, 모의투자를 켜는 순간 실계좌로 주문이 간다.
    """
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    monkeypatch.setenv("KIWOOM_ORDER_BASE", "https://api.kiwoom.com")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    assert not v.allowed
    assert any("실계좌로 주문이 나간다" in b for b in v.blockers)


def test_환경_라벨로는_속일_수_없다(db, monkeypatch):
    """**KIWOOM_ENV 는 읽는 코드가 없는 라벨이었다**(2026-09-01 실측).

    실제 서버는 URL 이 정한다. 라벨을 보고 판정하면 게이트가 거짓 안심을 준다.
    """
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    monkeypatch.setenv("KIWOOM_ENV", "mock")  # 라벨은 모의라고 말한다
    monkeypatch.setenv("KIWOOM_ORDER_BASE", "https://api.kiwoom.com")  # 실제는 실전
    assert not gcheck.evaluate(db, _decision(db), now=NOW).allowed


def test_주문_엔드포인트가_없으면_막는다(db, monkeypatch):
    """**조회용으로 조용히 대체하지 않는다** — 대체하는 순간 분리가 무의미해진다."""
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    monkeypatch.delenv("KIWOOM_ORDER_BASE", raising=False)
    monkeypatch.setenv("KIWOOM_REST_BASE", "https://api.kiwoom.com")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    assert not v.allowed and any("어디로 갈지 정해지지 않았다" in b for b in v.blockers)


def test_모의_판정은_kiwoom_모듈과_같은_기준을_쓴다():
    """두 곳이 다르게 판정하면 한쪽이 '모의'라고 믿는 동안 다른 쪽이 실전을 친다."""
    from data.sources.kiwoom import MOCK_HOST_MARK

    assert gcfg._is_mock_host(f"https://{MOCK_HOST_MARK}.kiwoom.com")
    assert not gcfg._is_mock_host("https://api.kiwoom.com")


def test_실계좌는_명시적_승인이_필요하다(db, monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("KIWOOM_ORDER_BASE", "https://api.kiwoom.com")
    assert any("AIK_LIVE_ACK" in b for b in gcheck.evaluate(db, _decision(db), now=NOW).blockers)

    monkeypatch.setenv("AIK_LIVE_ACK", "I_UNDERSTAND")
    assert gcheck.evaluate(db, _decision(db, "D2"), now=NOW).allowed


def test_모르는_모드는_paper_다(monkeypatch):
    """주문이 나가지 않는 쪽이 기본값이어야 한다."""
    for raw in ("", "LIVE_", "실전", "prod"):
        monkeypatch.setenv("EXECUTION_MODE", raw)
        assert gcfg.mode() == gcfg.PAPER


def test_paper_는_통과해도_주문이_나가지_않는다(db):
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    assert v.allowed and not v.sends_orders


def test_mock_은_주문이_나간다(db, monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    assert gcheck.evaluate(db, _decision(db), now=NOW).sends_orders


# ── 3. 결정 자격 ────────────────────────────────────────


def test_실험_결정은_집행하지_않는다(db):
    v = gcheck.evaluate(db, _decision(db, run_kind="experiment"), now=NOW)
    assert not v.allowed and any("실험 결정" in b for b in v.blockers)


@pytest.mark.parametrize("status", ["abstain", "schema_rejected", "api_error"])
def test_ok_가_아닌_결정은_집행하지_않는다(db, status):
    v = gcheck.evaluate(db, _decision(db, status=status), now=NOW)
    assert not v.allowed


def test_만료된_결정은_집행하지_않는다(db):
    past = (NOW - timedelta(hours=1)).isoformat()
    v = gcheck.evaluate(db, _decision(db, valid=past), now=NOW)
    assert not v.allowed and any("만료" in b for b in v.blockers)


def test_없는_결정은_차단이다(db):
    assert not gcheck.evaluate(db, "NOPE", now=NOW).allowed


# ── 4. 멱등성 ───────────────────────────────────────────


def test_같은_결정으로_두_번_주문하지_않는다(db):
    """어댑터가 응답을 못 줘도(타임아웃) 의도는 남아 재시도가 중복이 되지 않는다."""
    did = _decision(db, codes=("005930", "000660"))
    v1 = gcheck.evaluate(db, did, now=NOW)
    assert len(v1.orders) == 2
    gcheck.record(db, v1, now=NOW)

    v2 = gcheck.evaluate(db, did, now=NOW)
    assert v2.orders == ()
    assert not v2.allowed and any("이미 주문 의도" in b for b in v2.blockers)


def test_차단된_판정도_대장에_남는다(db, monkeypatch):
    """'왜 그날 주문이 안 나갔는가'를 나중에 물을 수 있어야 한다."""
    monkeypatch.setenv("KILL_SWITCH", "true")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    gcheck.record(db, v, now=NOW)
    rows = list(db.execute("SELECT status, reason FROM order_intents"))
    assert rows and rows[0][0] == "blocked" and "킬 스위치" in rows[0][1]


def test_대장이_모드와_서버를_남긴다(db, monkeypatch):
    """나중에 '이 주문이 어느 서버를 향했나'를 물을 수 있어야 한다."""
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    v = gcheck.evaluate(db, _decision(db), now=NOW)
    gcheck.record(db, v, now=NOW)
    row = db.execute("SELECT mode, kiwoom_env FROM order_intents").fetchone()
    assert row == ("mock", "mock")


def test_HOLD_는_주문이_아니다(db):
    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES ('H',?,1,'P','s',1,'premarket',?,?,'r1','ok',?)",
        (
            "live",
            NOW.isoformat(),
            (NOW + timedelta(hours=5)).isoformat(),
            json.dumps({"decisions": [{"action": "HOLD", "code": "005930", "name": "x"}]}),
        ),
    )
    assert gcheck.evaluate(db, "H", now=NOW).orders == ()


# ── 5. 게이트는 주문을 내지 않는다 ──────────────────────


def test_게이트에_주문_코드가_없다():
    """`execution/` 은 여전히 비어 있어야 한다 (하드 규칙 1·5).

    게이트를 만들면서 주문까지 같이 쓰면, CLAUDE.md 가 경고한
    "게이트를 처음 시험하는 자리가 주문을 내보내는 자리와 같다"가 그대로 재현된다.
    """
    import pathlib

    import gate

    root = pathlib.Path(gate.__file__).resolve().parent.parent
    assert not [p for p in (root / "execution").glob("*.py") if p.name != "__init__.py"]

    src = "\n".join(p.read_text(encoding="utf-8") for p in (root / "gate").glob("*.py"))
    for banned in ("kt10000", "주문전송", "place_order", "send_order"):
        assert banned not in src, f"게이트에 주문 코드가 들어왔다: {banned}"
