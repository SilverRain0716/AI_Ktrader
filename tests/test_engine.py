"""판단 엔진 — 팩 대조·재시도·기록이 실제로 동작하는지 (ADR 0007).

실제 API 는 부르지 않는다. 가짜 클라이언트로 **응답의 모든 모양**을 흘려보내
엔진이 각각을 어떻게 기록하는지 고정한다. 여기서 검사하는 것은 모델의 품질이 아니라
**엔진이 나쁜 응답을 통과시키지 않는가**다.
"""

from __future__ import annotations

import json

import pytest
from _fakes import FakeClient, _decision, _pack, _payload, _resp

from data import store
from decision import contract, engine

# ── 픽스처 ──────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    with store.connect(tmp_path / "t.db") as c:
        store.init_db(c)
        yield c


def _rows(conn) -> list[dict]:
    cur = conn.execute("SELECT * FROM decisions ORDER BY attempt")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


# ── 1. Arm 2 파생 ───────────────────────────────────────


def test_arm2_는_브리핑_전용_종목을_제거한다() -> None:
    """가리는 것이 아니라 없앤다 — F3 의 반증 행동이 '유니버스에서 제거'이기 때문이다."""
    out = engine.derive_arm2(_pack())
    codes = [u["code"] for u in out["universe"]]
    assert "035720" not in codes  # briefing 채널로만 들어왔다
    assert "005930" in codes  # momentum
    assert "000270" in codes  # briefing + flow → flow 로 남는다
    assert out["briefings"] == []


def test_arm2_는_남은_종목의_브리핑_흔적도_지운다() -> None:
    out = engine.derive_arm2(_pack())
    kia = next(u for u in out["universe"] if u["code"] == "000270")
    assert kia["channels"] == ["flow"]
    assert all(not r.startswith("briefing:") for r in kia["screen_reasons"])
    assert all("브리핑" not in w for w in out["data_quality"]["warnings"])


def test_arm2_파생은_정본_팩을_건드리지_않는다() -> None:
    """같은 팩에 대한 대응비교이므로 원본이 변하면 비교 자체가 성립하지 않는다."""
    p = _pack()
    before = contract.canonical_sha256(p)
    engine.derive_arm2(p)
    assert contract.canonical_sha256(p) == before


# ── 2. 렌더링 결정론 ────────────────────────────────────


def test_렌더링은_결정론적이다() -> None:
    """`render_input(pack) == 저장된 바이트` 가 재현 검사다. 흔들리면 검사가 의미를 잃는다."""
    p = _pack()
    assert engine.render_input(p, 1) == engine.render_input(p, 1)

    shuffled = json.loads(json.dumps({k: p[k] for k in reversed(list(p))}))
    assert engine.render_input(shuffled, 1) == engine.render_input(p, 1)


def test_arm_에_따라_입력이_다르다() -> None:
    p = _pack()
    assert engine.render_input(p, 1) != engine.render_input(p, 2)


# ── 3. 팩 대조 검사 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("decisions", "hint"),
    [
        ([_decision(code="999999")], "유니버스에 없다"),
        ([_decision(code="000660")], "이미 보유"),
        (
            [
                _decision(
                    action="EXIT",
                    code="005930",
                    entry=None,
                    stop=None,
                    weight_pct=None,
                    max_hold_days=None,
                )
            ],
            "보유 종목이 아니다",
        ),
        ([_decision(weight_pct=40.0)], "종목 한도"),
        ([_decision(), _decision(code="035720"), _decision(code="000270")], "신규 진입 3건"),
        ([_decision(briefing_refs=["없는브리핑"])], "입력에 없다"),
        (
            [
                _decision(
                    entry={"type": "LIMIT", "price": 90000, "condition": None, "valid_until": None}
                )
            ],
            "10% 넘게",
        ),
        ([_decision(stop={"type": "PRICE", "value": 80000})], "손절가"),
        ([_decision(target={"type": "PRICE", "value": 60000})], "목표가"),
    ],
)
def test_팩과_어긋나는_결정을_잡는다(decisions, hint) -> None:
    p = _pack()
    problems = engine.validate(_payload(decisions), p, 1)
    assert any(hint in x for x in problems), problems


def test_정상_결정은_통과한다() -> None:
    p = _pack()
    assert engine.validate(_payload(), p, 1) == []


def test_섹터_한도를_강제한다() -> None:
    """보유분과 신규를 합쳐 센다 — 신규만 보면 이미 찬 섹터에 더 담는다."""
    p = _pack()
    p["constraints"]["max_weight_pct_per_sector"] = 15.0
    # 000660(반도체) 10% 보유 + 005930(반도체) 10% 신규 = 20% > 15%
    assert any("섹터" in x for x in engine.validate(_payload(), p, 1))


def test_유동성_한도를_강제한다() -> None:
    p = _pack()
    p["constraints"]["max_order_vs_adv_pct"] = 0.001
    assert any("평균거래대금" in x for x in engine.validate(_payload(), p, 1))


def test_일일손실한도에_걸리면_신규진입을_막는다() -> None:
    p = _pack()
    p["constraints"]["daily_loss_limit_hit"] = True
    assert any("일일 손실" in x for x in engine.validate(_payload(), p, 1))


def test_당일_손절종목_재진입을_막는다() -> None:
    p = _pack()
    p["constraints"]["blocked_codes"] = ["005930"]
    assert any("손절한 종목" in x for x in engine.validate(_payload(), p, 1))


def test_arm2_에_briefing_refs_가_있으면_파생_누수다() -> None:
    """카나리아. 이것이 뜨면 derive_arm2 가 고장난 것이다 (ADR 0007 근거 3)."""
    p = engine.derive_arm2(_pack())
    p["briefings"] = [{"briefing_id": "b1"}]  # 일부러 되살려 참조 검사를 통과시킨다
    problems = engine.validate(_payload([_decision(briefing_refs=["b1"])]), p, 2)
    assert any("파생 누수" in x for x in problems)


# ── 4. decide() 의 응답 처리 ────────────────────────────


def test_정상_응답은_ok_로_기록된다(conn) -> None:
    client = FakeClient(_resp(_payload()))
    row = engine.decide(conn, _pack(), 1, client=client)

    assert row["status"] == "ok"
    assert row["attempt"] == 1
    assert row["decision_id"] == contract.decision_id("20260830-0929-premarket", 1)
    assert row["rendered_input"] == engine.render_input(_pack(), 1)  # 재현 검사
    assert row["pack_sha256"] == contract.canonical_sha256(_pack())
    assert len(_rows(conn)) == 1


def test_도구를_주지_않는다(conn) -> None:
    """도구가 있으면 모델이 팩 밖을 볼 수 있고, 그러면 F3 가 틀린 것을 잰다."""
    client = FakeClient(_resp(_payload()))
    engine.decide(conn, _pack(), 1, client=client)
    assert "tools" not in client.calls[0]


def test_출력_스키마를_API_에_넘긴다(conn) -> None:
    client = FakeClient(_resp(_payload()))
    engine.decide(conn, _pack(), 1, client=client)
    fmt = client.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"].keys() == engine._schema()["properties"].keys()


def test_abstain_은_ok_와_다른_status_다(conn) -> None:
    """abstain 은 관측이고 장애는 결측이다. 섞으면 F2·F3 표본이 오염된다."""
    client = FakeClient(_resp(_payload([], abstain=True, abstain_reason="후보 없음")))
    row = engine.decide(conn, _pack(), 1, client=client)
    assert row["status"] == "abstain"


def test_스키마_위반은_재요청하고_모든_시도가_남는다(conn) -> None:
    bad = _resp({"market_view": "x"})  # 필수 필드 누락
    client = FakeClient(bad, bad, _resp(_payload()))
    row = engine.decide(conn, _pack(), 1, client=client)

    rows = _rows(conn)
    assert [r["status"] for r in rows] == ["schema_rejected", "schema_rejected", "ok"]
    assert [r["attempt"] for r in rows] == [1, 2, 3]
    # 재시도가 같은 멱등키를 재사용해야 중복 주문 차단이 성립한다
    assert len({r["decision_id"] for r in rows}) == 1
    assert row["status"] == "ok"


def test_계약_위반은_사유와_함께_남는다(conn) -> None:
    client = FakeClient(_resp(_payload([_decision(code="999999")])))
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, client=client)

    rows = _rows(conn)
    assert len(rows) == engine.MAX_ATTEMPTS
    assert all(r["status"] == "contract_rejected" for r in rows)
    assert "유니버스에 없다" in rows[0]["problems"]


def test_API_장애는_abstain_이_아니라_api_error_다(conn) -> None:
    client = FakeClient(RuntimeError("connection reset"))
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, client=client)
    assert all(r["status"] == "api_error" for r in _rows(conn))


def test_잘린_응답을_부분_파싱하지_않는다(conn) -> None:
    """max_tokens 로 잘린 JSON 이 우연히 유효할 수 있다. stop_reason 을 먼저 본다."""
    truncated = _resp(_payload(), stop_reason="max_tokens")
    client = FakeClient(truncated)
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, client=client)
    rows = _rows(conn)
    assert all("max_tokens" in r["problems"] for r in rows)
    assert all(r["payload"] is None for r in rows)


def test_거부_응답은_판단이_아니다(conn) -> None:
    client = FakeClient(_resp(_payload(), stop_reason="refusal"))
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, client=client)
    assert all(r["status"] == "api_error" for r in _rows(conn))


def test_깨진_JSON_은_원문이_남는다(conn) -> None:
    """11.6 의 교훈 — 파서가 버린 것을 원문으로 되찾을 수 있어야 한다."""
    client = FakeClient(_resp(None, raw="{이건 JSON 이 아니다"))
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, client=client)
    assert all(r["raw_response"] == "{이건 JSON 이 아니다" for r in _rows(conn))


def test_감시_불가한_invalidation_이_세어진다(conn) -> None:
    """비율이 높아지면 enum 을 재검토한다. 세지 않으면 알 수 없다."""
    decisions = [
        _decision(),
        _decision(
            code="000270",
            invalidation={"type": "unstructured", "value": None, "deadline": None, "text": "느낌"},
        ),
    ]
    client = FakeClient(_resp(_payload(decisions)))
    row = engine.decide(conn, _pack(), 1, client=client)
    assert (row["monitorable"], row["unmonitorable"]) == (1, 1)


def test_봉인_필드는_러너가_채운다(conn) -> None:
    """모델이 무엇을 보내든 멱등키·팩 참조·타임스탬프는 러너 값이다."""
    client = FakeClient(_resp(_payload()))
    row = engine.decide(conn, _pack(), 1, client=client)
    for field in ("decision_id", "pack_id", "arm", "model", "prompt_sha256", "render_version"):
        assert row[field] is not None
    assert row["prompt_sha256"] == contract.canonical_sha256(engine.prompt_text())


# ── 5. 짝 판단 ──────────────────────────────────────────


def test_한쪽_arm_이_실패하면_쌍에서_제외된다(conn) -> None:
    """짝 없는 관측을 쌍으로 세면 McNemar 가 오염된다."""

    class ArmAware(FakeClient):
        def _create(self, **kw):
            self.calls.append(kw)
            # Arm 2 입력에는 브리핑 전용 종목이 없다 — 그것으로 arm 을 구분한다
            if "035720" not in kw["messages"][0]["content"]:
                raise RuntimeError("arm2 실패")
            return _resp(_payload())

    out = engine.decide_pair(conn, _pack(), client=ArmAware())
    assert out["paired"] is False
    assert out["arm1"] is not None
    assert out["arm2"] is None


def test_양쪽_성공이면_쌍이다(conn) -> None:
    out = engine.decide_pair(conn, _pack(), client=FakeClient(_resp(_payload([]))))
    assert out["paired"] is True
    assert {r["arm"] for r in _rows(conn)} == {1, 2}


# ── 6. 저장 계층 ────────────────────────────────────────


def test_같은_시도를_두_번_기록할_수_없다(conn) -> None:
    """append-only 라도 (decision_id, attempt) 는 유일하다 — 중복 기록은 감사 왜곡이다."""
    import sqlite3

    client = FakeClient(_resp(_payload()))
    row = engine.decide(conn, _pack(), 1, client=client)
    with pytest.raises(sqlite3.IntegrityError):
        engine.save_decision(conn, row)


def test_API_키가_없으면_분명하게_멈춘다(conn, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(engine.DecisionRefused, match="ANTHROPIC_API_KEY"):
        engine.decide(conn, _pack(), 1)


def test_키가_없으면_쌍_실패가_아니라_설정_문제로_올라간다(conn, monkeypatch) -> None:
    """설정 부재가 '양쪽 arm 실패'로 보이면 사람은 판단이 나빴다고 읽는다.

    원인(설정)과 증상(판단 없음)이 뒤바뀌는 것을 막는다 — CLI 종료 코드도 3 과 4 로 갈린다.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(engine.DecisionRefused, match="ANTHROPIC_API_KEY"):
        engine.decide_pair(conn, _pack())
    assert _rows(conn) == []  # 시도조차 하지 않았으므로 기록도 없다


def test_짝_판단은_클라이언트를_한_번만_만든다(conn) -> None:
    client = FakeClient(_resp(_payload([])))
    engine.decide_pair(conn, _pack(), client=client)
    assert len(client.calls) == 2  # arm 1·2 각각 한 번


def test_arm_0_은_아직_없다(conn) -> None:
    """Arm 0 의 랭킹 규칙이 정의되지 않았다. 임의로 정하면 그것이 기준선이 된다."""
    with pytest.raises(engine.DecisionRefused, match="arm=0"):
        engine.pack_for_arm(_pack(), 0)


def test_결정_만료는_접속매매_종료_시각이다(conn) -> None:
    """15:20 이후는 종가 단일가라 연속 체결이 없다 — 지정가·조건부 진입이 의도대로 안 된다.

    설계안 v1 4.3 실행 창과 4.4 스키마 예시가 둘 다 15:20 이다. 한때 15:30 이었고,
    그 10분은 시간 차이가 아니라 **체결 방식의 차이**다.
    """
    from datetime import datetime

    from data import config as dcfg

    for cycle in ("premarket", "midday", "preclose", "event"):
        assert engine.CYCLE_VALID_UNTIL[cycle] == (15, 20), cycle

    client = FakeClient(_resp(_payload()))
    pack = {**_pack(), "cycle": "premarket"}
    row = engine.decide(
        conn, pack, 1, client=client, now=datetime(2026, 8, 31, 8, 20, tzinfo=dcfg.KST)
    )
    assert row["valid_until"].startswith("2026-08-31T15:20")
