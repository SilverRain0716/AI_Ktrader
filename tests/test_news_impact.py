"""뉴스 파급도 판정 계약 (ADR 0012).

여기서 지키는 것 넷.

1. **스키마가 strict 구조화 출력에 계속 적합할 것.** 부적합해지면 `output_config.format`
   으로 넘길 수 없고, 형식 강제가 조용히 사라진다 — 결정 스키마와 같은 규칙이다.
2. **없는 숫자를 만들어내지 못할 것.** `scale_raw` 는 제목의 부분문자열이어야 한다.
   실측에서 정규식이 틀린 방식이 정확히 이것이었다.
3. **항목이 조용히 빠지지 않을 것.** 100건을 보내 97건이 오면 3건은 사라진 것이다.
4. **파급도는 시총 대비일 것.** 절대 금액으로 되돌아가는 경로를 막는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from decision import news_impact as ni

SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "schemas" / "news_impact.schema.json").read_text(
        encoding="utf-8"
    )
)


# ── 1. strict 구조화 출력 적합성 ────────────────────────


def _strict_issues(node, path="root") -> list[str]:
    issues: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
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
    broken = json.loads(json.dumps(SCHEMA))
    del broken["$defs"]["judgment"]["additionalProperties"]
    assert _strict_issues(broken)


def test_조건부_검증_문법을_쓰지_않는다() -> None:
    """strict 디코더 지원 여부를 확인하지 못한 문법이다 (결정 스키마와 같은 이유)."""
    raw = json.dumps(SCHEMA)
    for kw in ('"if"', '"then"', '"allOf"', '"oneOf"', '"anyOf"'):
        assert kw not in raw, f"{kw} 를 쓰고 있다"


def test_봉인_필드는_모델_출력에_없다() -> None:
    """파급도(impact_pct)가 모델 출력에 있으면 검산할 대상이 사라진다."""
    raw = json.dumps(SCHEMA)
    for f in ni.ENVELOPE_FIELDS:
        assert f'"{f}"' not in raw, f"{f} 는 러너가 봉인한다"


def test_스키마_자체가_유효하다() -> None:
    Draft202012Validator.check_schema(SCHEMA)


# ── 2. 1단계 깔때기 — 실측 사례 ─────────────────────────


@pytest.mark.parametrize(
    ("headline", "names", "ok"),
    [
        ("삼성중공업, 컨테이너운반선 2척 4445억원 수주", True, True),
        ("풍력터빈 기업 유니슨, 고창해상풍력에 1천억원 규모 공급", True, True),
        ("삼성바이오로직스, 3조원대 유상증자 발표에 6%대↓[특징주]", True, True),
        ("연준 의장 '매파' 본색에…코스피 2%대 하락", False, False),
        ("가온전선, 싱가포르 600억 수주", True, True),
        # 규모어도 금액도 없다
        ("이동훈 SK바이오팜 사장, 오파칼림에 힘 싣는다", True, False),
    ],
)
def test_깔때기는_넓게_거른다(headline, names, ok) -> None:
    assert ni.passes_funnel(headline, names) is ok


def test_SK온_사례는_탈락한다() -> None:
    """**이 ADR 을 촉발한 사례가 자기 규칙에 걸린다.**

    제목이 'SK온'이라 SK이노베이션(096770) 페이지에서 `names_stock=False` 다.
    계열사 사전이 없어 감수하는 비용이고(ADR 0012 결정 7), 사전이 생기면 이 테스트가
    바뀌어야 한다. **모르고 놓치는 것과 알고 놓치는 것을 구분하려고 못박는다.**
    """
    h = "[속보] SK온, 美 네오볼타에 5년간 9GWh LFP 배터리 공급"
    assert ni.passes_funnel(h, names_stock=False) is False
    assert ni.passes_funnel(h, names_stock=True) is True, "규모어·물량은 잡혀야 한다"


# ── 3. item_id — 리플레이 가능성 ────────────────────────


def test_같은_입력은_같은_id_를_준다() -> None:
    a = ni.item_id("096770", "SK이노베이션, 1.5조 공급계약", "2026-08-31T08:04")
    b = ni.item_id("096770", "SK이노베이션, 1.5조 공급계약", "2026-08-31T08:04:00+09:00")
    assert a == b, "초·오프셋 표기 차이로 id 가 갈리면 리플레이에서 판정이 안 붙는다"


def test_종목이_다르면_id_가_다르다() -> None:
    """같은 기사가 여러 종목 페이지에 뜬다 — 판정은 종목별로 달라야 한다."""
    h, at = "SK하닉 5조 꽂은 이유는", "2026-08-28T09:00"
    assert ni.item_id("042700", h, at) != ni.item_id("000660", h, at)


# ── 4. 지어낸 숫자 차단 — 여기가 핵심이다 ───────────────


def _payload(**over):
    j = {
        "item_id": "0123456789ab",
        "code": "096770",
        "attributed": True,
        "subject": "SK온",
        "modality": "confirmed",
        "sign": "positive",
        "scale_raw": "1.5조",
        "scale_eok_krw": 15000,
        "scope": "stock",
        "note": None,
    }
    j.update(over)
    return {"judgments": [j], "data_concerns": []}


ITEMS = {"0123456789ab": "SK온, 네오볼타에 9GWh 공급…1.5조 규모"}


def test_정상_판정은_통과한다() -> None:
    assert ni.check_payload(_payload(), ITEMS) == []


def test_제목에_없는_금액은_거부한다() -> None:
    """실측에서 정규식이 틀린 방식이다 — 남의 금액을 이 종목 것으로 읽었다."""
    bad = ni.check_payload(_payload(scale_raw="8조", scale_eok_krw=80000), ITEMS)
    assert bad and "지어낸" in bad[0]


def test_근거_없이_금액만_오면_거부한다() -> None:
    assert ni.check_payload(_payload(scale_raw=None, scale_eok_krw=15000), ITEMS)


def test_단위_착오를_잡는다() -> None:
    """'조'를 억으로 읽으면 만 배다. 접미사가 곧 단위라는 규칙(CLAUDE.md)이다."""
    assert ni.check_payload(_payload(scale_eok_krw=1.5), ITEMS)
    assert ni.check_payload(_payload(), ITEMS) == [], "정상값까지 잡으면 검사가 무의미하다"


def test_물량에는_금액을_채우지_않는다() -> None:
    """'9GWh' 에 단가를 곱하면 그 순간 환각이다."""
    assert ni.check_payload(_payload(scale_raw="9GWh", scale_eok_krw=15000), ITEMS)
    assert ni.check_payload(_payload(scale_raw="9GWh", scale_eok_krw=None), ITEMS) == []


def test_규모_표현이_없어도_된다() -> None:
    assert ni.check_payload(_payload(scale_raw=None, scale_eok_krw=None), ITEMS) == []


# ── 5. 항목이 조용히 빠지지 않는다 ──────────────────────


def test_판정되지_않은_항목을_잡는다() -> None:
    items = dict(ITEMS, cafebabe0001="다른 기사 제목")
    bad = ni.check_payload(_payload(), items)
    assert bad and "판정되지 않은" in bad[0]


def test_보내지_않은_항목이_돌아오면_잡는다() -> None:
    bad = ni.check_payload(_payload(item_id="ffffffffffff"), ITEMS)
    assert any("보내지 않은" in b for b in bad)


def test_중복_판정을_잡는다() -> None:
    p = _payload()
    p["judgments"].append(dict(p["judgments"][0]))
    assert any("중복" in b for b in ni.check_payload(p, ITEMS))


# ── 6. 파급도는 시총 대비다 ─────────────────────────────


def test_파급도는_시총_대비다() -> None:
    """SK이노베이션 실측: 1.5조 / 시총 19.76조 = 7.6%."""
    assert ni.impact_pct(15000, 197623) == pytest.approx(7.59, abs=0.05)


def test_같은_금액도_회사_크기에_따라_다르다() -> None:
    """절대 금액으로 되돌아가면 삼성전자의 1.5조와 중소형주의 1.5조가 같아진다."""
    small = ni.impact_pct(15000, 30000)
    big = ni.impact_pct(15000, 5_000_000)
    assert small is not None and big is not None
    assert small > 40 and big < 1


@pytest.mark.parametrize(("scale", "cap"), [(None, 197623), (15000, None), (15000, 0)])
def test_못_재면_None_이지_0_이_아니다(scale, cap) -> None:
    """0 으로 채우면 '재료가 없다' 와 '쟀는데 작다' 가 같아진다."""
    assert ni.impact_pct(scale, cap) is None


def test_컷을_두지_않는다() -> None:
    """'파급도 N% 이상이면 후보'를 지금 정하면 고정 % 를 세 번째로 틀리는 것이다.

    실험 8 이 정할 때까지 이 모듈은 값을 계산할 뿐 걸러내지 않는다 (ADR 0012 결정 4).
    """
    src = (Path(ni.__file__)).read_text(encoding="utf-8")
    assert "MIN_IMPACT" not in src and "IMPACT_THRESHOLD" not in src
