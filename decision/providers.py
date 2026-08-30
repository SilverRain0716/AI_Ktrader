"""LLM 제공자 어댑터 — Claude 와 GPT 를 같은 계약으로 부른다.

## 왜 추상화하는가, 그리고 무엇을 하면 안 되는가

제공자를 바꿀 수 있게 하는 것과 **바꿔도 되는 것**은 다르다.

- [ADR 0005](../docs/adr/0005-backtest-scope.md) 의 3-arm 은 `Arm 1 − Arm 2` 차이를 브리핑의
  증분으로 읽는다. 두 arm 을 **다른 제공자로 돌리면 그 차이에 제공자 차이가 섞여** F3 가
  틀린 것을 잰다. 그래서 `engine.decide_pair()` 가 한 쌍 안에서 제공자·모델 고정을 강제한다.
- [ADR 0007](../docs/adr/0007-judgment-engine.md) 의 동결 정책상 페이퍼 기간 중 제공자 교체는
  **모델 교체와 같은 급의 함수 변경**이다. 표본이 `(provider, model, prompt_id)` 층으로 갈린다.

즉 이 모듈의 목적은 "섞어 쓰기"가 아니라 **갈아끼우기**다. 그리고 갈아끼운 사실이
결정 행에 남는다.

## 제공자별로 다른 것

| | Anthropic | OpenAI |
|---|---|---|
| 메서드 | `messages.create` | `responses.create` |
| 스키마 강제 | `output_config.format` | `text.format` (`strict: True`) |
| 중단 사유 | `stop_reason` | `status` + `incomplete_details.reason` |

**OpenAI strict 모드는 `pattern`·`minLength`·`minimum`·`format` 을 강제하지 않는다** —
구조만 잡고 값은 보지 않는다. 그런데 `engine.decide()` 가 응답을 받은 뒤 로컬 `jsonschema`
로 전체를 다시 검증하므로 **정확성은 제공자와 무관하다.** 달라지는 것은 재시도율이다.
"강제됐다는 말과 강제됐는지 확인하는 것은 다르다"가 여기서 값을 한다.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

# 정규화된 중단 사유. 엔진은 이 값만 본다.
END_TURN = "end_turn"
MAX_TOKENS = "max_tokens"
REFUSAL = "refusal"


@dataclass(frozen=True)
class Reply:
    """제공자 응답을 한 모양으로 맞춘 것. 엔진은 원본 객체를 보지 않는다."""

    raw: str
    stop_reason: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Provider(Protocol):
    name: str
    default_model: str

    def call(self, *, model: str, system: str, user: str, schema: dict, params: dict) -> Reply: ...


class MissingCredential(RuntimeError):
    """제공자 자격증명이 없다. 판단의 부재가 아니라 설정 문제다."""


# ── Anthropic ───────────────────────────────────────────


class AnthropicProvider:
    name = "anthropic"
    default_model = "claude-opus-5"
    # 은퇴 보장 하한 2027-07-24 (문서 실측). 3-arm 표본이 9.5개월을 요구하므로
    # 이 날짜에서 역산한 페이퍼 착수 마감이 2026-10-09 다 — ADR 0008 참조.
    env_key = "ANTHROPIC_API_KEY"

    def __init__(self, client=None):
        self._client = client

    def _ensure(self):
        if self._client is not None:
            return self._client
        if not os.getenv(self.env_key):
            raise MissingCredential(f"{self.env_key} 가 설정되지 않았다.")
        import anthropic

        self._client = anthropic.Anthropic()
        return self._client

    def call(self, *, model: str, system: str, user: str, schema: dict, params: dict) -> Reply:
        client = self._ensure()
        body: dict[str, Any] = dict(params)
        body["output_config"] = {
            **body.get("output_config", {}),
            "format": {"type": "json_schema", "schema": schema},
        }
        t0 = time.monotonic()
        r = client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            **body,
        )
        return Reply(
            raw=next((b.text for b in r.content if b.type == "text"), ""),
            stop_reason=r.stop_reason,
            request_id=getattr(r, "_request_id", None),
            input_tokens=r.usage.input_tokens,
            output_tokens=r.usage.output_tokens,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


# ── OpenAI ──────────────────────────────────────────────


class OpenAIProvider:
    name = "openai"
    default_model = "gpt-5.6"
    env_key = "OPENAI_API_KEY"

    # Anthropic 파라미터 이름을 그대로 넘기면 400 이 난다. 옮길 것만 옮긴다.
    _PARAM_MAP: ClassVar[dict[str, str]] = {"max_tokens": "max_output_tokens"}
    # 제공자 고유 파라미터 — 넘기지 않는다
    _DROP: ClassVar[set[str]] = {"thinking", "output_config"}

    def __init__(self, client=None):
        self._client = client

    def _ensure(self):
        if self._client is not None:
            return self._client
        if not os.getenv(self.env_key):
            raise MissingCredential(f"{self.env_key} 가 설정되지 않았다.")
        import openai

        self._client = openai.OpenAI()
        return self._client

    def _translate(self, params: dict) -> dict:
        out = {}
        for k, v in params.items():
            if k in self._DROP:
                continue
            out[self._PARAM_MAP.get(k, k)] = v
        # effort 는 Anthropic 쪽에서 output_config 안에 있었다. 옮겨 준다.
        effort = (params.get("output_config") or {}).get("effort")
        if effort:
            out["reasoning"] = {"effort": effort}
        return out

    def call(self, *, model: str, system: str, user: str, schema: dict, params: dict) -> Reply:
        client = self._ensure()
        t0 = time.monotonic()
        r = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "decision",
                    "strict": True,
                    "schema": schema,
                }
            },
            **self._translate(params),
        )
        return Reply(
            raw=_openai_text(r),
            stop_reason=_openai_stop(r),
            request_id=getattr(r, "id", None),
            input_tokens=getattr(r.usage, "input_tokens", 0),
            output_tokens=getattr(r.usage, "output_tokens", 0),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


def _openai_text(r) -> str:
    """`output_text` 가 있으면 그것을, 없으면 output 블록을 훑는다."""
    text = getattr(r, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(r, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", None) in ("output_text", "text"):
                parts.append(c.text)
    return "".join(parts)


def _openai_stop(r) -> str:
    """중단 사유를 Anthropic 어휘로 정규화한다.

    **모르는 상태를 `end_turn` 으로 만들지 않는다.** 그러면 잘린 응답이 정상으로 읽히고,
    엔진의 "부분 파싱 금지"가 무력화된다 — 통과와 실패가 구분되지 않는 바로 그 형태다.
    """
    for item in getattr(r, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", None) == "refusal":
                return REFUSAL
    status = getattr(r, "status", None)
    if status == "completed":
        return END_TURN
    if status == "incomplete":
        reason = getattr(getattr(r, "incomplete_details", None), "reason", "")
        return MAX_TOKENS if "token" in str(reason) else f"incomplete:{reason}"
    return f"status:{status}"


# ── 선택 ────────────────────────────────────────────────

_REGISTRY: dict[str, type] = {
    AnthropicProvider.name: AnthropicProvider,
    OpenAIProvider.name: OpenAIProvider,
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str | None = None, *, client=None) -> Provider:
    """제공자를 만든다. 이름이 없으면 `AIK_LLM_PROVIDER`, 그것도 없으면 anthropic.

    **자격증명을 여기서 확인한다.** 첫 호출까지 미루면 "키가 없다"가 재시도 루프 안에서
    API 장애로 기록되고, 사람은 "판단이 3번 실패했다"로 읽는다 — 원인과 증상이 뒤바뀐다.
    (실제로 한 번 그렇게 됐다. 그 회귀를 막는 테스트가 tests/test_engine.py 에 있다.)
    """
    name = (name or os.getenv("AIK_LLM_PROVIDER") or AnthropicProvider.name).lower()
    if name not in _REGISTRY:
        raise MissingCredential(f"모르는 제공자 '{name}'. 가능한 값: {', '.join(available())}")
    p = _REGISTRY[name](client=client)
    p._ensure()  # 만드는 시점에 부를 수 있는지 확정한다
    return p


def resolve_model(provider: Provider, model: str | None = None) -> str:
    """모델 이름. 없으면 `AIK_LLM_MODEL`, 그것도 없으면 제공자 기본값."""
    return model or os.getenv("AIK_LLM_MODEL") or provider.default_model
