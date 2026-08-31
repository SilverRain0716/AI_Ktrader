"""뉴스 파급도 계약 — 스키마가 못 보는 것을 보고, 파급도를 계산한다.

[ADR 0012](../docs/adr/0012-news-channel.md) 가 정한 것이 여기 있다.

## 왜 스키마만으로 부족한가

`news_impact.schema.json` 은 **형식만** 본다. 형식을 지키면서 틀릴 수 있는 것이 셋이다.

1. **없는 숫자를 만들어낸다.** `scale_raw` 가 제목에 없는 문자열이어도 스키마는 통과한다.
   실측에서 정규식이 틀린 방식이 정확히 이것이었다 — 남의 금액을 이 종목 것으로 읽었다.
   그래서 **`scale_raw` 가 제목의 부분문자열인지 대조**한다.
2. **항목이 조용히 빠진다.** 100건을 주고 97건이 오면 3건은 판정되지 않은 채 사라진다.
   `item_id` **집합이 정확히 같은지** 본다 — 결정 엔진이 팩과 결정을 대조하는 것과 같다.
3. **환산이 틀린다.** `scale_raw="1.5조"` 인데 `scale_eok_krw=1.5` 면 만 배 어긋난다.
   억원 접미사 규칙(CLAUDE.md)이 지켜지는지 **자릿수만** 검산한다.

## 파급도는 여기서 계산한다 — 모델이 하지 않는다

**파급도 = `scale_eok_krw` / `market_cap_eok_krw`.** 절대 금액이 아니라 시총 대비다.
ATR 배수([ADR 0009](../docs/adr/0009-entry-timing.md))·거래대금 배수([ADR 0011](../docs/adr/0011-event-scan.md))에
이어 **같은 교훈이 세 번째로 나온 자리**다.

모델에 시총을 주지 않는 이유는 둘이다. 비율은 검산 가능한 산술이므로 코드가 맡고,
시총을 보여주면 *"큰 회사니 큰 재료겠지"* 하는 역방향 추론이 판정에 섞인다.

**컷은 여기 없다.** "파급도 몇 % 이상이면 후보"를 지금 정하면 [ADR 0009](../docs/adr/0009-entry-timing.md)·
[ADR 0011](../docs/adr/0011-event-scan.md) 에서 고정 % 를 두 번 틀린 것과 같은 실수가 된다.
실험 8 이 정할 때까지 이 모듈은 **값을 계산할 뿐 걸러내지 않는다.**
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# 러너가 봉인하는 필드. 스키마(모델 출력)에 이 이름이 나타나면 계약 분리가 무너진 것이다.
ENVELOPE_FIELDS = (
    "judgment_id",
    "as_of",
    "model",
    "provider",
    "generated_at",
    "impact_pct",  # 파급도는 산술이다 — 모델이 내면 검산할 대상이 사라진다
)

# 1단계 기계 깔때기 (ADR 0012 결정 4). **넓게 거른다** — 정밀도는 2단계 AI 가 본다.
# 실측(2026-08-28, 31종목 93건): 통과율 11.8% → 662종목 환산 하루 약 235건.
SCALE_WORDS = (
    "수주|계약|공급|납품|승인|허가|인수|합병|증설|투자|출시|선정|체결|낙찰|"
    "유상증자|무상증자|자사주|배당|특허|임상|허가신청"
)
_FUNNEL = re.compile(
    r"[0-9][0-9.,]*\s*(?:조|억)"  # 금액
    r"|[0-9][0-9.,]*\s*(?:GWh|MWh|MW|㎿|톤|만대|만개|만t|기)\b"  # 물량
    rf"|{SCALE_WORDS}"
)


def passes_funnel(headline: str, names_stock: bool) -> bool:
    """1단계. **놓치지 않는 것이 목적이고 맞히는 것이 아니다.**

    `names_stock` 을 요구하므로 *"SK온, 美 네오볼타에 9GWh 공급"* 은 **탈락한다** —
    이 ADR 을 촉발한 사례가 자기 규칙에 걸린다. 계열사 사전이 생기기 전까지
    감수하는 비용이고, 그런 항목은 관측 통로로 따로 남긴다(ADR 0012 결정 7).
    """
    return bool(names_stock) and bool(_FUNNEL.search(headline))


def item_id(code: str, headline: str, at: str) -> str:
    """(종목, 제목, 시각) 의 결정론적 해시.

    순번이 아니라 해시인 이유: **리플레이에서 같은 입력이 같은 id 를 가져야** 저장된
    판정을 다시 붙일 수 있다. 순번은 그날 수집 순서가 바뀌면 어긋난다.
    """
    raw = f"{code}\x1f{_norm(headline)}\x1f{at[:16]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _norm(s: str) -> str:
    """전각·기호 표기 흔들림을 없앤다. 매체마다 '…' 과 '...' 이 섞인다."""
    return unicodedata.normalize("NFKC", s).strip()


def _digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s)


def check_payload(payload: dict, items: dict[str, str]) -> list[str]:
    """모델 응답의 계약 위반을 전부 모아 돌려준다. 빈 리스트면 통과다.

    `items` 는 러너가 보낸 것: `{item_id: headline}`. **응답이 아니라 요청이 정본이다.**
    """
    problems: list[str] = []
    judgments = payload.get("judgments") or []

    got = [j.get("item_id") for j in judgments]
    dupes = {i for i in got if got.count(i) > 1}
    if dupes:
        problems.append(f"item_id 중복 {sorted(dupes)}")

    missing = set(items) - set(got)
    extra = set(got) - set(items)
    if missing:
        problems.append(f"판정되지 않은 항목 {len(missing)}건 {sorted(missing)[:5]}")
    if extra:
        problems.append(f"보내지 않은 항목이 돌아왔다 {sorted(extra)[:5]}")

    for j in judgments:
        iid = j.get("item_id")
        headline = items.get(iid)
        if headline is None:
            continue  # extra 로 이미 잡혔다
        problems += _check_scale(j, headline, iid)
    return problems


def _check_scale(j: dict, headline: str, iid: str) -> list[str]:
    raw, eok = j.get("scale_raw"), j.get("scale_eok_krw")
    out: list[str] = []

    if raw is not None and _norm(raw) not in _norm(headline):
        out.append(f"{iid}: scale_raw '{raw}' 가 제목에 없다 — 지어낸 숫자다")
    if eok is not None and raw is None:
        out.append(f"{iid}: 근거 문자열 없이 금액만 왔다 (scale_eok_krw={eok})")
    if eok is None or raw is None:
        return out

    # 자릿수 검산. **정확한 값이 아니라 단위 착오만 잡는다** — '조'를 억으로 읽으면 만 배다.
    # 접미사 `_eok_krw` 가 곧 단위라는 규칙(CLAUDE.md)이 지켜지는지 보는 것이다.
    digits = _digits(raw)
    if not digits:
        # 통화 표현이 아닌데(예: '9GWh') 금액을 채웠다
        out.append(f"{iid}: '{raw}' 는 통화 금액이 아닌데 scale_eok_krw 가 채워졌다")
        return out
    unit = 10000 if "조" in raw else 1
    lo = float(digits[:4] or 0) * unit / 1000  # 소수점·자릿수 흔들림 여유
    hi = float(digits[:4] or 0) * unit * 1000
    if not (lo <= eok <= hi) and eok != 0:
        out.append(f"{iid}: '{raw}' → {eok} 억원은 자릿수가 맞지 않는다")
    return out


def impact_pct(scale_eok_krw: float | None, market_cap_eok_krw: float | None) -> float | None:
    """**파급도.** 시총 대비 재료 규모(%). 어느 한쪽이 없으면 None — 0 이 아니다.

    0 으로 채우면 *"재료가 없다"* 와 *"쟀는데 작다"* 가 같아진다. 이 저장소가
    반복해서 당한 실패 방식이다 — **못 받은 것을 받은 척하지 않는다.**
    """
    if not scale_eok_krw or not market_cap_eok_krw or market_cap_eok_krw <= 0:
        return None
    return round(scale_eok_krw / market_cap_eok_krw * 100, 3)


__all__ = [
    "ENVELOPE_FIELDS",
    "check_payload",
    "impact_pct",
    "item_id",
    "passes_funnel",
]
