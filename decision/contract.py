"""결정 계약 — 스키마가 못 보는 것을 보고, 러너가 봉인하는 것을 만든다.

[ADR 0007](../docs/adr/0007-judgment-engine.md) 이 정한 세 가지가 여기 있다.

1. **봉인(envelope)** — `decision_id`·`pack_id`·`arm`·`model`·타임스탬프는 모델이 아니라
   러너가 만든다. 멱등키를 확률적 텍스트 생성기가 만들면 재시도마다 값이 달라져
   "같은 id 의 주문은 두 번 나가지 않는다"가 성립하지 않는다.
2. **action 별 필수 조건** — 예전에는 스키마의 `allOf` + `if/then` 이 강제했다.
   strict 구조화 출력이 그 문법을 지원하지 않아 스키마에서 뺐고, **규칙을 잃지 않으려고
   여기로 옮겼다.** 스키마에서 사라진 것이 아니라 위치가 바뀐 것이다.
3. **감시 가능성** — `invalidation.type == "unstructured"` 는 기계가 감시할 수 없다.
   조용히 통과시키지 않고 `monitorable=False` 로 드러낸다.

여기에 유니버스 대조·산술 재검증(한도 강제)은 **없다.** 그것은 판단 엔진(Phase 4)이
팩을 들고 하는 일이고, 이 모듈은 팩 없이도 판정할 수 있는 것만 다룬다.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# 모델이 만들지 않고 러너가 봉인하는 필드. 스키마(모델 출력)에 이 이름이 나타나면
# 계약 분리가 무너진 것이다 — 테스트가 그것을 막는다.
ENVELOPE_FIELDS = (
    "decision_id",
    "pack_id",
    "pack_sha256",
    "arm",
    "cycle",
    "model",
    "generated_at",
    "valid_until",
)

# BUY/ADD 는 손절 없이 진입할 수 없다. 예전 스키마 allOf[0] 이 강제하던 규칙이다.
_REQUIRED_BY_ACTION: dict[str, tuple[str, ...]] = {
    "BUY": ("weight_pct", "entry", "stop", "max_hold_days"),
    "ADD": ("weight_pct", "entry", "stop", "max_hold_days"),
    "TRIM": ("weight_pct",),  # 예전 allOf[1]
}

# 이 action 들은 보유분에 대한 지시이므로 신규 진입 필드가 있으면 안 된다.
_FORBIDDEN_BY_ACTION: dict[str, tuple[str, ...]] = {
    "HOLD": ("entry",),
    "EXIT": ("entry", "weight_pct"),
}

# 기계가 감시할 수 있는 조건. **수급 타입이 먼저다** — ADR 0013 원칙 2 가
# "% 가 아니라 수급과 재료 소멸로 판단한다" 이기 때문이다.
MONITORABLE_TYPES = frozenset(
    {
        "flow_reversal",  # 수급 이탈 — 원칙 2 의 본체
        "volume_dryup",  # 관심 소멸
        "disclosure_category",
        "stance_reversal",
        "price_below",
        "close_below_ma",
    }
)


def canonical_sha256(payload: Any) -> str:
    """내용 해시. 키 순서·공백에 흔들리지 않아야 같은 팩이 같은 해시를 낸다."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# 실험 라벨에 허용하는 문자. id 를 파싱할 수 있어야 하므로 구분자(-)를 막는다.
_RUN_LABEL = re.compile(r"^[A-Za-z0-9_.]{1,32}$")


def decision_id(pack_id: str, arm: int, run: str | None = None) -> str:
    """`(pack_id, arm)` 당 하나. **결정론적이어야 재시도가 같은 키를 재사용한다.**

    난수를 쓰면 재시도마다 다른 멱등키가 나와 중복 주문 차단이 무의미해진다.

    `run` 은 **실험 라벨**이다. 주면 접미사가 붙어 같은 팩을 여러 번 판단할 수 있다 —
    프롬프트 A/B 와 모델 변동성 측정이 그것 없이는 불가능했다(UNIQUE 제약에 막힌다).
    **`run` 이 붙은 결정은 집행 대상이 아니다**(`run_kind='experiment'`).

    라벨에 `-` 를 막는 이유는 id 를 되짚을 때 팩 id 와 경계가 흐려지기 때문이다.
    """
    base = f"{pack_id}-a{arm}"
    if run is None:
        return base
    if not _RUN_LABEL.match(run):
        raise ValueError(
            f"실험 라벨 {run!r} 은 영숫자·밑줄·점 1~32자여야 한다 (하이픈 불가) — "
            "id 를 되짚을 때 팩 id 와 경계가 흐려진다"
        )
    return f"{base}-x{run}"


def is_monitorable(invalidation: dict) -> bool:
    """실행 계층이 감시 조건으로 등록할 수 있는가."""
    return invalidation.get("type") in MONITORABLE_TYPES


def action_requirements(decision: dict) -> list[str]:
    """action 별 필수·금지 필드를 검사한다. 위반 목록을 돌려준다 (빈 목록 = 통과).

    스키마는 필드가 **존재하는지**만 본다 — 전 필드가 required 이고 null 이 허용되므로
    `stop: null` 인 BUY 도 스키마는 통과시킨다. 그 구멍을 여기서 막는다.
    """
    action = decision.get("action")
    problems: list[str] = []

    for field in _REQUIRED_BY_ACTION.get(action, ()):
        if decision.get(field) is None:
            problems.append(f"{action} 인데 {field} 가 null 이다")

    for field in _FORBIDDEN_BY_ACTION.get(action, ()):
        if decision.get(field) is not None:
            problems.append(f"{action} 인데 {field} 가 채워져 있다")

    entry = decision.get("entry")
    if entry:
        if entry.get("type") == "LIMIT" and entry.get("price") is None:
            problems.append("entry.type=LIMIT 인데 price 가 null 이다")
        if entry.get("type") == "COND" and not entry.get("condition"):
            problems.append("entry.type=COND 인데 condition 이 비어 있다")
        if entry.get("type") == "MARKET" and entry.get("price") is not None:
            problems.append("entry.type=MARKET 인데 price 가 채워져 있다")

    inv = decision.get("invalidation") or {}
    inv_type = inv.get("type")
    val = inv.get("value")
    if inv_type in ("price_below", "close_below_ma") and not isinstance(val, (int, float)):
        problems.append(f"invalidation.type={inv_type} 인데 value 가 숫자가 아니다")
    if inv_type == "disclosure_category" and not isinstance(val, str):
        problems.append("invalidation.type=disclosure_category 인데 value 가 문자열이 아니다")
    # 수급 타입 (ADR 0013 원칙 2). 범위를 여기서 막지 않으면 value=0 인 flow_reversal 이
    # "0일 연속 순매도"가 되어 진입 즉시 참이 된다 — 스키마는 형만 보고 값을 못 본다.
    if inv_type == "flow_reversal" and not (isinstance(val, (int, float)) and val >= 1):
        problems.append(
            "invalidation.type=flow_reversal 인데 value 가 1 이상 정수가 아니다 (연속 순매도 일수)"
        )
    if inv_type == "volume_dryup" and not (isinstance(val, (int, float)) and 0 < val < 1):
        problems.append(
            "invalidation.type=volume_dryup 인데 value 가 0 초과 1 미만이 아니다 (거래대금 배수)"
        )

    return problems


def check_payload(payload: dict) -> list[str]:
    """모델 출력 전체에 대한 계약 검사. 스키마 검증을 통과한 뒤에 돌린다."""
    problems: list[str] = []

    if payload.get("abstain") and not payload.get("abstain_reason"):
        problems.append("abstain=true 인데 abstain_reason 이 비어 있다")
    if not payload.get("abstain") and payload.get("abstain_reason"):
        problems.append("abstain=false 인데 abstain_reason 이 채워져 있다")

    # abstain 은 "신규 진입을 하지 않는다"는 뜻이다. 보유분 지시(HOLD/TRIM/EXIT)는 계속 낼 수 있다.
    if payload.get("abstain"):
        entries = [d for d in payload.get("decisions", []) if d.get("action") in ("BUY", "ADD")]
        if entries:
            problems.append(f"abstain=true 인데 신규 진입 {len(entries)}건이 있다")

    seen: set[str] = set()
    for i, d in enumerate(payload.get("decisions", [])):
        code = d.get("code")
        if code in seen:
            problems.append(f"decisions[{i}] 종목 {code} 가 중복이다")
        seen.add(code)
        problems += [f"decisions[{i}] {p}" for p in action_requirements(d)]

    return problems
