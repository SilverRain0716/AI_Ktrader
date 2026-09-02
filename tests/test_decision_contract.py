"""결정 계약 — 스키마 개정과 팩 불변화가 실제로 막는지 검사한다 (ADR 0007 선행 조치).

이 파일이 지키는 것은 셋이다.

1. **스키마가 strict 구조화 출력에 계속 적합할 것.** 부적합해지면 `output_config.format` 에
   넘기지 못하고, 그러면 생산자와 검증자가 다시 갈라진다.
2. **스키마에서 뺀 `allOf` 규칙이 코드에서 살아 있을 것.** 위치를 옮긴 것이지 없앤 것이 아니다.
   "손절 없는 BUY 가 통과한다"로 되돌아가는 경로를 막는다.
3. **팩이 불변일 것.** 결정이 팩을 참조하는데 팩이 덮이면 감사 사슬의 첫 고리가 끊긴다.

각 검사는 **위반이 실제로 잡히는지**를 본다. 통과하는 것만 보면 검사기가 죽어 있어도 초록불이다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from decision import contract
from decision.config import PackImmutable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "decision.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
# 개별 결정만 검증할 때 쓴다. $defs 를 함께 넘겨야 내부 $ref 가 풀린다.
DECISION_SCHEMA = {**SCHEMA["$defs"]["decision"], "$defs": SCHEMA["$defs"]}


# ── 1. strict 구조화 출력 적합성 ────────────────────────


def _strict_issues(node, path="root") -> list[str]:
    """모든 객체가 additionalProperties=false 이고 모든 속성이 required 인가."""
    issues: list[str] = []
    if isinstance(node, dict):
        if "properties" in node:
            if node.get("additionalProperties") is not False:
                issues.append(f"{path}: additionalProperties 미설정")
            missing = set(node["properties"]) - set(node.get("required") or [])
            if missing:
                issues.append(f"{path}: required 누락 {sorted(missing)}")
        for k, v in node.items():
            issues += _strict_issues(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            issues += _strict_issues(v, f"{path}[{i}]")
    return issues


def test_스키마는_구조화출력에_그대로_넘길_수_있다() -> None:
    assert _strict_issues(SCHEMA) == []


def test_적합성_검사기가_실제로_위반을_잡는다() -> None:
    """검사기가 죽어 있으면 위 테스트는 항상 통과한다. 그것을 막는다."""
    broken = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert _strict_issues(broken)


def test_조건부_검증_문법을_쓰지_않는다() -> None:
    """if/then·allOf·oneOf 는 strict 디코더 지원 여부를 확인하지 못했다. 쓰지 않는다."""
    blob = json.dumps(SCHEMA)
    for keyword in ('"if"', '"allOf"', '"oneOf"', '"anyOf"'):
        assert keyword not in blob, f"{keyword} 가 스키마에 있다"


def test_봉인_필드는_모델_출력에_없다() -> None:
    """decision_id 같은 멱등키를 모델이 만들면 재시도마다 값이 달라진다 (ADR 0007 근거 4).

    원문 문자열이 아니라 **속성 키**를 본다 — `entry.valid_until` 은 진입 지시의
    유효 시각이라 모델이 내는 것이 맞다. 결정 전체의 `valid_until` 과 이름만 같다.
    """
    top = set(SCHEMA["properties"])
    per_decision = set(SCHEMA["$defs"]["decision"]["properties"])
    for field in contract.ENVELOPE_FIELDS:
        assert field not in top, f"{field} 가 팩 최상위에 있다 — 러너가 봉인해야 한다"
        assert field not in per_decision, f"{field} 가 decision 에 있다 — 러너가 봉인해야 한다"


def test_스키마_자체가_유효하다() -> None:
    Draft202012Validator.check_schema(SCHEMA)


# ── 2. 스키마에서 코드로 옮긴 규칙 ──────────────────────


def _buy(**over) -> dict:
    d = {
        "action": "BUY",
        "code": "005930",
        "name": None,
        "weight_pct": 10.0,
        "rank": 1,
        "entry": {"type": "MARKET", "price": None, "condition": None, "valid_until": None},
        "stop": {"type": "ATR", "value": 2.0},
        "target": None,
        "trail": None,
        "max_hold_days": 10,
        "confidence": "중",
        "reasons": ["정배열 RSI 65", "기관 3일 순매수"],
        "invalidation": {
            "type": "close_below_ma",
            "value": 20,
            "deadline": None,
            "text": "20일선 종가 이탈",
        },
        "briefing_refs": [],
        "sources": [],
    }
    d.update(over)
    return d


def test_정상_BUY_는_스키마와_계약을_모두_통과한다() -> None:
    payload = {
        "market_view": "코스피 20일선 위.",
        "abstain": False,
        "abstain_reason": None,
        "decisions": [_buy()],
        "portfolio_note": None,
        "data_concerns": [],
    }
    assert list(Draft202012Validator(SCHEMA).iter_errors(payload)) == []
    assert contract.check_payload(payload) == []


def test_손절_없는_BUY_는_스키마를_통과하지만_계약이_막는다() -> None:
    """전 필드 required + nullable 이라 `stop: null` 인 BUY 도 스키마는 통과한다.

    예전 `allOf[0]` 이 막던 것이다. 그 규칙이 코드로 옮겨 왔는지를 여기서 고정한다.
    """
    bad = _buy(stop=None)
    assert list(Draft202012Validator(DECISION_SCHEMA).iter_errors(bad)) == []  # 스키마는 통과
    assert any("stop" in p for p in contract.action_requirements(bad))  # 계약은 거부


@pytest.mark.parametrize(
    ("over", "hint"),
    [
        ({"weight_pct": None}, "weight_pct"),
        ({"entry": None}, "entry"),
        ({"max_hold_days": None}, "max_hold_days"),
        ({"action": "TRIM", "entry": None, "stop": None, "weight_pct": None}, "weight_pct"),
        ({"action": "HOLD", "stop": None}, "entry"),
        ({"action": "EXIT", "entry": None, "stop": None}, "weight_pct"),
    ],
)
def test_action_별_필수_금지_필드(over, hint) -> None:
    problems = contract.action_requirements(_buy(**over))
    assert any(hint in p for p in problems), problems


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "LIMIT", "price": None, "condition": None, "valid_until": None},
        {"type": "COND", "price": None, "condition": "", "valid_until": None},
        {"type": "MARKET", "price": 70000, "condition": None, "valid_until": None},
    ],
)
def test_진입_방식과_필드가_어긋나면_잡는다(entry) -> None:
    assert contract.action_requirements(_buy(entry=entry))


@pytest.mark.parametrize(
    "inv",
    [
        {"type": "price_below", "value": "싸지면", "deadline": None, "text": "x"},
        {"type": "close_below_ma", "value": None, "deadline": None, "text": "x"},
        {"type": "disclosure_category", "value": 3, "deadline": None, "text": "x"},
    ],
)
def test_invalidation_의_value_형이_type_과_맞아야_한다(inv) -> None:
    assert contract.action_requirements(_buy(invalidation=inv))


# ── 3. 감시 가능성 ──────────────────────────────────────


def test_unstructured_invalidation_은_감시되지_않는다고_표시된다() -> None:
    """조용히 통과시키지 않는 것이 요점이다 — 감시 안 되는 조건은 드러나야 한다."""
    assert not contract.is_monitorable({"type": "unstructured"})
    for t in contract.MONITORABLE_TYPES:
        assert contract.is_monitorable({"type": t})


# ── 4. payload 수준 계약 ────────────────────────────────


def test_abstain_인데_신규진입이_있으면_거부한다() -> None:
    payload = {
        "market_view": "관망.",
        "abstain": True,
        "abstain_reason": "근거 있는 후보가 없다",
        "decisions": [_buy()],
        "portfolio_note": None,
        "data_concerns": [],
    }
    assert any("신규 진입" in p for p in contract.check_payload(payload))


def test_abstain_인데_이유가_없으면_거부한다() -> None:
    payload = {
        "market_view": "관망.",
        "abstain": True,
        "abstain_reason": None,
        "decisions": [],
        "portfolio_note": None,
        "data_concerns": [],
    }
    assert any("abstain_reason" in p for p in contract.check_payload(payload))


def test_같은_종목이_두_번_나오면_거부한다() -> None:
    payload = {
        "market_view": "x",
        "abstain": False,
        "abstain_reason": None,
        "decisions": [_buy(), _buy()],
        "portfolio_note": None,
        "data_concerns": [],
    }
    assert any("중복" in p for p in contract.check_payload(payload))


def test_abstain_이어도_보유분_지시는_낼_수_있다() -> None:
    """abstain 은 '신규 진입 안 함'이지 '아무 말도 안 함'이 아니다."""
    payload = {
        "market_view": "x",
        "abstain": True,
        "abstain_reason": "신규 후보 없음",
        "decisions": [
            _buy(action="EXIT", entry=None, stop=None, weight_pct=None, max_hold_days=None)
        ],
        "portfolio_note": None,
        "data_concerns": [],
    }
    assert contract.check_payload(payload) == []


# ── 5. 멱등키 ──────────────────────────────────────────


def test_decision_id_는_결정론적이다() -> None:
    """재시도가 같은 키를 재사용해야 '같은 id 의 주문은 두 번 나가지 않는다'가 성립한다."""
    a = contract.decision_id("20260830-0929-premarket", 1)
    b = contract.decision_id("20260830-0929-premarket", 1)
    assert a == b
    assert a != contract.decision_id("20260830-0929-premarket", 2)


def test_canonical_해시는_키_순서에_흔들리지_않는다() -> None:
    assert contract.canonical_sha256({"a": 1, "b": 2}) == contract.canonical_sha256(
        {"b": 2, "a": 1}
    )
    assert contract.canonical_sha256({"a": 1}) != contract.canonical_sha256({"a": 2})


# ── 6. 팩 불변화 ───────────────────────────────────────


def _store_pack(conn, pack_id: str, universe_size: int) -> dict:
    return {
        "pack_id": pack_id,
        "cycle": "premarket",
        "generated_at": "2026-08-30T09:29:00+09:00",
        "universe": [{"code": f"{i:06d}"} for i in range(universe_size)],
        "positions": [],
        "briefings": [],
        "data_quality": {"warnings": []},
    }


def test_같은_내용_재빌드는_통과한다(tmp_path) -> None:
    from data import store
    from decision import pack as packmod

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        p = _store_pack(conn, "20260830-0929-premarket", 3)
        packmod.save(conn, p)
        packmod.save(conn, p)  # 같은 내용 — 무해하다
        n = conn.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0]
        assert n == 1


def test_내용이_다른_덮어쓰기는_거부한다(tmp_path) -> None:
    """이것이 ADR 0007 이 막으려는 것이다 — 결정이 참조하는 근거가 조용히 바뀌는 경로."""
    from data import store
    from decision import pack as packmod

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        packmod.save(conn, _store_pack(conn, "20260830-0929-premarket", 3))
        with pytest.raises(PackImmutable):
            packmod.save(conn, _store_pack(conn, "20260830-0929-premarket", 5))

        # 원래 내용이 그대로 남아 있어야 한다 — 거부했는데 값이 바뀌면 거부가 아니다
        row = conn.execute(
            "SELECT payload FROM context_packs WHERE pack_id=?", ("20260830-0929-premarket",)
        ).fetchone()
        assert len(json.loads(row[0])["universe"]) == 3


# ── 5. 실험 라벨 (프롬프트 A/B · 변동성 측정) ───────────


def test_실험_라벨이_없으면_멱등키가_그대로다():
    """주문 멱등키를 건드리면 안 된다 — 같은 팩·arm 에 집행 대상이 둘이면 중복 주문이다."""
    assert contract.decision_id("20260901-0018-premarket", 1) == "20260901-0018-premarket-a1"


def test_실험_라벨이_붙으면_다른_키가_된다():
    """이것이 없어서 같은 팩을 두 프롬프트로 비교할 수 없었다 — UNIQUE 제약에 막혔다."""
    a = contract.decision_id("P1", 1)
    b = contract.decision_id("P1", 1, "v3run1")
    c = contract.decision_id("P1", 1, "v3run2")
    assert len({a, b, c}) == 3


@pytest.mark.parametrize("bad", ["has-hyphen", "", "a" * 33, "공백 있음", "slash/x"])
def test_잘못된_실험_라벨을_거부한다(bad):
    """하이픈을 막는 이유는 id 를 되짚을 때 팩 id 와 경계가 흐려지기 때문이다."""
    with pytest.raises(ValueError):
        contract.decision_id("P1", 1, bad)


# ── 6. 종목코드 형식 ────────────────────────────────────


def test_문자가_섞인_종목코드를_받는다():
    """**한국 종목코드는 숫자 6자리만이 아니다.**

    실측(2026-09-01): 삼성에피스홀딩스 0126Z0 · 에임드바이오 0009K0 ·
    삼양바이오팜 0120G0 · 한화머시너리앤서비스홀딩스 0220W0 — 전부 보통주다.
    숫자만 허용하면 이들이 유니버스에서 조용히 빠진다. 실제로 팩 생성이 거부됐다.
    """
    import re

    pat = SCHEMA["$defs"]["stockCode"]["pattern"]
    for code in ("005930", "0126Z0", "0009K0", "0120G0", "0220W0"):
        assert re.fullmatch(pat, code), code
    for bad in ("00593", "0059300", "00593a", "KOSDAQ"):
        assert not re.fullmatch(pat, bad), bad
