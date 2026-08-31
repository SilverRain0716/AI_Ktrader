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
    ("headline", "ok"),
    [
        ("삼성중공업, 컨테이너운반선 2척 4445억원 수주", True),
        ("풍력터빈 기업 유니슨, 고창해상풍력에 1천억원 규모 공급", True),
        ("삼성바이오로직스, 3조원대 유상증자 발표에 6%대↓[특징주]", True),
        ("가온전선, 싱가포르 600억 수주", True),
        ("연준 의장 '매파' 본색에…코스피 2%대 하락", False),
        ("이동훈 SK바이오팜 사장, 오파칼림에 힘 싣는다", False),
    ],
)
def test_깔때기는_넓게_거른다(headline, ok) -> None:
    assert ni.passes_funnel(headline) is ok


def test_계열사_기사를_버리지_않는다() -> None:
    """**이 ADR 을 촉발한 사례다.** 처음에 names_stock 을 함께 요구해 탈락시켰다.

    제목이 'SK온'이라 SK이노베이션 페이지에서 names_stock=False 인데, 게이트로 쓰면
    ADR 0010 의 *"거르지 않고 표시만 한다"* 를 스스로 어기게 된다.
    """
    assert ni.passes_funnel("[속보] SK온, 美 네오볼타에 5년간 9GWh LFP 배터리 공급")


def test_약칭_기사를_버리지_않는다() -> None:
    """실측에서 names_stock 게이트가 버린 것들. 증권 기사는 약칭을 쓴다."""
    assert ni.passes_funnel("삼바, M&A 위해 3조원 유증 … 항체·mRNA 이어 영역 확대")
    assert ni.passes_funnel("한화에어로 항공사업 속도..미국 엔진부품 자회사에 2200억 수혈")


def test_깔때기는_종목명을_요구하지_않는다() -> None:
    """게이트로 되돌아가는 경로를 막는다 — 인자로 받지도 않는다."""
    import inspect

    assert "names_stock" not in inspect.signature(ni.passes_funnel).parameters


# ── 2-2. 계열사 귀속과 지분율 ───────────────────────────

AFFIL = {"에스케이온(주) (주1,2)": 90.3, "에스케이지오센트릭(주) (주1)": 100.0}
OTHERS = {"SK하이닉스": "000660", "삼성전자": "005930"}


def test_음차로_계열사를_찾는다() -> None:
    """DART 는 '에스케이온(주)', 기사는 'SK온' 이라고 쓴다."""
    assert ni.resolve_ownership("SK온", "SK이노베이션", AFFIL, OTHERS) == (0.903, "dart")


def test_음차가_과적용되지_않는다() -> None:
    """'지오' 를 GO 로 바꾸면 'SKGO센트릭' 이 되어 기사의 'SK지오센트릭' 과 어긋난다.

    접두 길이별 변형을 전부 만들어 그중 하나가 맞게 한다.
    """
    assert ni.resolve_ownership("SK지오센트릭", "SK이노베이션", AFFIL, OTHERS)[1] == "dart"


def test_종목_자신은_self_다() -> None:
    assert ni.resolve_ownership("SK이노베이션", "SK이노베이션", AFFIL, OTHERS) == (1.0, "self")


def test_다른_상장사_재료는_파급도를_계산하지_않는다() -> None:
    """실측된 오판: 한미반도체 페이지의 'SK하닉 5조 꽂은 이유는'.

    AI 가 attributed 를 잘못 내도 남의 5조가 이 종목 파급도가 되면 안 된다.
    """
    own, src = ni.resolve_ownership("SK하이닉스", "SK이노베이션", AFFIL, OTHERS)
    assert (own, src) == (None, "foreign")
    assert ni.impact_pct(50000, 197623, own) is None


def test_못_가리면_가정했다고_남긴다() -> None:
    """약칭 '삼바' 는 사전으로 못 푼다. 1.0 을 쓰되 근거를 self 라고 속이지 않는다."""
    assert ni.resolve_ownership("삼바", "삼성바이오로직스", {}, OTHERS) == (1.0, "assumed")


def test_지분율을_못_읽으면_None_이다() -> None:
    """0 으로도 1.0 으로도 채우지 않는다 — 못 읽은 것이 100% 가 되면 안 된다."""
    own, src = ni.resolve_ownership("SK온", "SK이노베이션", {"에스케이온(주)": None}, OTHERS)
    assert own is None and src == "dart"
    assert ni.impact_pct(15000, 197623, own) is None


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


def test_자회사_재료는_지분율로_줄인다() -> None:
    """SK온 1.5조 × 지분 90.3% / 시총 19.76조 = 6.85%. 미조정이면 7.59% 다.

    **근사다** — 수주액은 매출이고 지분율은 순이익 귀속 비율이다. 지분 30% 관계회사의
    재료를 100% 로 세는 것보다 낫다는 뜻이지 정밀한 계산이라는 뜻이 아니다.
    """
    assert ni.impact_pct(15000, 197623, 0.903) == pytest.approx(6.85, abs=0.02)
    assert ni.impact_pct(15000, 197623, 1.0) == pytest.approx(7.59, abs=0.02)


def test_지분율이_None_이면_계산하지_않는다() -> None:
    assert ni.impact_pct(15000, 197623, None) is None


def test_컷을_두지_않는다() -> None:
    """'파급도 N% 이상이면 후보'를 지금 정하면 고정 % 를 세 번째로 틀리는 것이다.

    실험 8 이 정할 때까지 이 모듈은 값을 계산할 뿐 걸러내지 않는다 (ADR 0012 결정 4).
    """
    src = (Path(ni.__file__)).read_text(encoding="utf-8")
    assert "MIN_IMPACT" not in src and "IMPACT_THRESHOLD" not in src


# ── 7. 자회사 사전 저장 왕복 ────────────────────────────


def test_자회사_사전은_최신_사업연도만_쓴다(tmp_path) -> None:
    """지분율은 시점 값이다. 연도를 섞으면 같은 회사가 두 값을 갖는다."""
    from data import store

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        store.upsert_affiliates(
            conn,
            [
                {
                    "corp_code": "C1",
                    "inv_prm": "에스케이온(주)",
                    "quota_rt": 88.0,
                    "bsns_year": "2024",
                },
                {
                    "corp_code": "C1",
                    "inv_prm": "에스케이온(주)",
                    "quota_rt": 90.3,
                    "bsns_year": "2025",
                },
            ],
            code="096770",
        )
        aff = store.affiliates_of(conn, "096770")

    assert aff == {"에스케이온(주)": 90.3}


def test_지분율_결측은_None_으로_남는다(tmp_path) -> None:
    """0 으로 떨어뜨리면 '재료가 모회사와 무관하다' 는 뜻이 되어버린다."""
    from data import store

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        store.upsert_affiliates(
            conn,
            [
                {
                    "corp_code": "C1",
                    "inv_prm": "이름만있는법인",
                    "quota_rt": None,
                    "bsns_year": "2025",
                }
            ],
            code="096770",
        )
        assert store.affiliates_of(conn, "096770") == {"이름만있는법인": None}


def test_사전이_없으면_빈_dict_다(tmp_path) -> None:
    from data import store

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        assert store.affiliates_of(conn, "096770") == {}


@pytest.mark.parametrize(
    ("raw", "want"), [("90.3", 90.3), ("1,000", 1000.0), ("-", None), ("", None), (None, None)]
)
def test_지분율_파싱은_0_으로_떨어지지_않는다(raw, want) -> None:
    from data.sources import dart

    assert dart._pct(raw) == want
