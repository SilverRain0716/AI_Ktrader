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


def _decision(conn, did="P-a1", *, run_kind="live", status="ok", valid=None, codes=("005930",)):
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
    # mock/live 는 예수금 확인이 필수다 — 없으면 시드가 잔고를 넘는지 알 수 없다.
    assert gcheck.evaluate(db, _decision(db, "P2-a1"), now=NOW, deposit_krw=10**12).allowed


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
    assert gcheck.evaluate(db, _decision(db), now=NOW, deposit_krw=10**12).sends_orders


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
    assert not gcheck.evaluate(db, "NOPE-a1", now=NOW).allowed


# ── 4. 멱등성 ───────────────────────────────────────────


def test_같은_결정으로_두_번_주문하지_않는다(db):
    """어댑터가 응답을 못 줘도(타임아웃) 의도는 남아 재시도가 중복이 되지 않는다.

    **다만 판정 기록만으로는 막지 않는다** — `allowed` 는 아직 주문이 안 나간 상태다.
    주문이 나간 뒤(`sent` 이상)부터 막는다.
    """
    did = _decision(db, codes=("005930", "000660"))
    v1 = gcheck.evaluate(db, did, now=NOW)
    assert len(v1.orders) == 2
    gcheck.record(db, v1, now=NOW)
    db.execute("UPDATE order_intents SET status='sent'")  # 어댑터가 주문을 냈다

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


# ── 6. 시드와 계좌 예수금 ───────────────────────────────


def test_시드가_예수금보다_크면_막는다(db, monkeypatch):
    """주문이 거부되거나 미수가 남는다."""
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    monkeypatch.setenv("AIK_PAPER_EQUITY_KRW", "500000000")
    v = gcheck.evaluate(db, _decision(db), now=NOW, deposit_krw=20_000_000)
    assert not v.allowed and any("예수금" in b and "크다" in b for b in v.blockers)


def test_예수금을_확인하지_못하면_막는다(db, monkeypatch):
    """**확인 실패와 잔고 부족을 섞지 않는다.** 0 으로 접으면 둘이 같아진다."""
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    v = gcheck.evaluate(db, _decision(db), now=NOW, deposit_krw=None)
    assert not v.allowed and any("확인하지 못했다" in b for b in v.blockers)


def test_시드가_예수금보다_작으면_막지_않고_알린다(db, monkeypatch):
    """실측(2026-09-01): 모의계좌 예수금 5억 vs 페이퍼 시드 2천만.

    주문은 시드 기준으로 나가므로 안전하다. 다만 **계좌 수익률로 성적을 읽으면 어긋난다** —
    막을 일은 아니고 드러낼 일이다.
    """
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    monkeypatch.setenv("AIK_PAPER_EQUITY_KRW", "20000000")
    v = gcheck.evaluate(db, _decision(db), now=NOW, deposit_krw=500_000_000)
    assert v.allowed, "작은 시드는 막을 이유가 없다"
    assert any("어긋난다" in n for n in v.notes)


def test_paper_는_계좌를_보지_않는다(db, monkeypatch):
    """계좌를 건드리지 않는 모드에서 예수금을 요구하면 돌지 않는다."""
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    assert gcheck.evaluate(db, _decision(db), now=NOW, deposit_krw=None).allowed


def test_예수금_파싱은_0으로_접지_않는다():
    """'entr' 을 못 읽었는데 0 을 돌려주면 모든 주문이 시드 초과로 막힌다."""
    from gate import account as gacct

    class Fake:
        def __init__(self, v):
            self.v = v

        def post(self, *a, **k):
            return {"return_code": 0, "entr": self.v}

    assert gacct.deposit_krw(Fake("000000500000000")) == 500_000_000
    assert gacct.deposit_krw(Fake("")) is None
    assert gacct.deposit_krw(Fake("알수없음")) is None


def test_판정만_기록된_것은_중복이_아니다(db, monkeypatch):
    """**기록됨과 주문됨은 다르다.**

    처음에 order_intents 에 행이 있기만 하면 중복으로 봤다. 그 결과
    `check --record` 뒤에 `place` 를 부르면 **항상 막혔다**(2026-09-01 실측).
    멱등성이 보호하는 것은 판정 기록이 아니라 **중복 주문**이다.
    """
    did = _decision(db)
    v1 = gcheck.evaluate(db, did, now=NOW)
    gcheck.record(db, v1, now=NOW)  # allowed 로 남는다

    v2 = gcheck.evaluate(db, did, now=NOW)
    assert v2.allowed, "판정만 기록됐는데 막혔다"
    assert len(v2.orders) == len(v1.orders)


@pytest.mark.parametrize("status", ["sent", "filled", "gapped", "expired"])
def test_주문이_나간_뒤에는_막는다(db, status):
    """어댑터가 응답을 못 줘도(타임아웃) 재시도가 중복 주문이 되면 안 된다."""
    did = _decision(db)
    gcheck.record(db, gcheck.evaluate(db, did, now=NOW), now=NOW)
    db.execute("UPDATE order_intents SET status=?", (status,))
    v = gcheck.evaluate(db, did, now=NOW)
    assert not v.allowed and any("이미 주문" in b for b in v.blockers)


# ── 7. arm → 계좌 매핑 (ADR 0014) ───────────────────────


@pytest.mark.parametrize(
    ("did", "arm"),
    [("20260901-1117-midday-a1", 1), ("P-a2", 2), ("P-a1-xvar1", 1)],
)
def test_결정_id_에서_arm_을_읽는다(did, arm):
    assert gcfg.arm_of(did) == arm


@pytest.mark.parametrize("bad", ["P", "P-arm1", "20260901-midday", ""])
def test_arm_을_못_읽으면_거부한다(bad):
    """추측하면 arm 1 의 주문이 계좌 2 로 나간다 — 두 계좌가 동시에 오염된다."""
    with pytest.raises(gcfg.GateConfigError):
        gcfg.arm_of(bad)


def test_arm_마다_다른_계좌를_쓴다(monkeypatch):
    monkeypatch.setenv("KIWOOM_MOCK_APP_KEY", "AAA")
    monkeypatch.setenv("KIWOOM_MOCK_APP_SECRET", "aaa")
    monkeypatch.setenv("KIWOOM_MOCK2_APP_KEY", "BBB")
    monkeypatch.setenv("KIWOOM_MOCK2_APP_SECRET", "bbb")
    assert gcfg.credentials_for(1) == ("AAA", "aaa")
    assert gcfg.credentials_for(2) == ("BBB", "bbb")


def test_자격증명이_없으면_다른_계좌로_대체하지_않는다(monkeypatch):
    """**대체하는 순간 두 arm 이 같은 계좌를 쓴다** — 주문이 섞여 F3 를 영영 못 잰다."""
    monkeypatch.setenv("KIWOOM_MOCK_APP_KEY", "AAA")
    monkeypatch.setenv("KIWOOM_MOCK_APP_SECRET", "aaa")
    monkeypatch.delenv("KIWOOM_MOCK2_APP_KEY", raising=False)
    with pytest.raises(gcfg.GateConfigError, match="대체하지 않는다"):
        gcfg.credentials_for(2)


def test_배정되지_않은_arm_은_차단한다():
    """Arm 0(정량 랭킹)은 아직 0줄이라 계좌가 없다 — 시뮬레이터로 남긴다."""
    with pytest.raises(gcfg.GateConfigError, match="배정된 계좌가 없다"):
        gcfg.credentials_for(0)


def test_대장에_arm_이_남는다(db, monkeypatch):
    """나중에 '이 주문이 어느 계좌로 갔나'를 물을 수 있어야 한다."""
    monkeypatch.setenv("EXECUTION_MODE", "mock")
    v = gcheck.evaluate(db, _decision(db, "P-a2"), now=NOW, deposit_krw=10**12)
    gcheck.record(db, v, now=NOW)
    assert db.execute("SELECT arm FROM order_intents").fetchone()[0] == 2


def test_조건부_진입은_집행하지_않는다(db):
    """**ADR 0009 가 정해놓고 거부하는 코드가 없었다**(2026-09-02 발견).

    `contract.py` 는 "COND 면 condition 이 있어야 한다"만 보고, 게이트는 `entry.type` 을
    아예 안 봤다. 어댑터만 붙으면 **감시 못 하는 조건부 주문이 그대로 나간다.**

    **모의계좌라도 막는다** — 위험이 없는 것과 판정할 수 없는 것은 다르다.
    """
    import json as _j

    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES ('C-a1','live',1,'P','s',1,'premarket',?,?,'r1','ok',?)",
        (
            NOW.isoformat(),
            (NOW + timedelta(hours=5)).isoformat(),
            _j.dumps(
                {
                    "decisions": [
                        {
                            "action": "BUY",
                            "code": "096770",
                            "name": "SK이노베이션",
                            "weight_pct": 10,
                            "entry": {"type": "COND", "condition": "거래대금 평균 회복"},
                        },
                        {
                            "action": "BUY",
                            "code": "005930",
                            "name": "삼성전자",
                            "weight_pct": 5,
                            "entry": {"type": "MARKET", "price": None},
                        },
                    ]
                }
            ),
        ),
    )
    v = gcheck.evaluate(db, "C-a1", now=NOW)
    assert not v.allowed
    assert any("COND" in b and "블록 G" in b for b in v.blockers)
    assert [o["code"] for o in v.orders] == ["005930"], "MARKET 은 남아야 한다"


def test_허용_목록은_팩이_정본이다(db):
    """게이트가 따로 상수를 들고 있으면 **팩이 말한 것과 게이트가 막는 것이 갈라진다** —
    AI 는 팩을 보고 판단했는데 다른 기준으로 차단된다.
    """
    import json as _j

    db.execute(
        "INSERT INTO context_packs (pack_id,cycle,generated_at,universe_size,position_count,"
        "view_count,warning_count,payload) VALUES ('PK','premarket',?,1,0,0,0,?)",
        (NOW.isoformat(), _j.dumps({"constraints": {"allowed_entry_types": ["MARKET", "COND"]}})),
    )
    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES ('K-a1','live',1,'PK','s',1,'premarket',?,?,'r1','ok',?)",
        (
            NOW.isoformat(),
            (NOW + timedelta(hours=5)).isoformat(),
            _j.dumps(
                {
                    "decisions": [
                        {
                            "action": "BUY",
                            "code": "096770",
                            "name": "x",
                            "weight_pct": 5,
                            "entry": {"type": "COND", "condition": "c"},
                        },
                        {
                            "action": "BUY",
                            "code": "005930",
                            "name": "y",
                            "weight_pct": 5,
                            "entry": {"type": "LIMIT", "price": 100},
                        },
                    ]
                }
            ),
        ),
    )
    v = gcheck.evaluate(db, "K-a1", now=NOW)
    # 팩이 COND 를 허용했으므로 통과, LIMIT 은 허용 목록에 없으므로 차단
    assert [o["code"] for o in v.orders] == ["096770"]
    assert any("LIMIT" in b for b in v.blockers)


def test_팩이_없으면_기본_허용목록을_쓴다(db):
    """조용히 전부 통과시키지 않는다 — 없으면 코드의 기본값으로 막는다."""
    import json as _j

    from decision import config as ccfg

    db.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,payload) "
        "VALUES ('N-a1','live',1,'NOPACK','s',1,'premarket',?,?,'r1','ok',?)",
        (
            NOW.isoformat(),
            (NOW + timedelta(hours=5)).isoformat(),
            _j.dumps(
                {
                    "decisions": [
                        {
                            "action": "BUY",
                            "code": "096770",
                            "name": "x",
                            "weight_pct": 5,
                            "entry": {"type": "COND"},
                        }
                    ]
                }
            ),
        ),
    )
    assert "COND" not in ccfg.ALLOWED_ENTRY_TYPES
    assert not gcheck.evaluate(db, "N-a1", now=NOW).allowed


def test_팩이_허용_진입방식을_싣는다():
    """팩에 없는 것은 AI 에게 없다 (ADR 0003 원칙 1)."""
    import inspect

    from decision import pack as pk

    assert "allowed_entry_types" in inspect.getsource(pk.build)
