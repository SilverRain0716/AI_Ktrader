"""제공자 어댑터 — 갈아끼울 수 있고, 섞이지 않는다.

두 가지를 지킨다.

1. **같은 계약으로 부른다.** 엔진은 제공자 원본 객체를 보지 않는다 — `Reply` 만 본다.
2. **한 쌍 안에서 섞이지 않는다.** `Arm 1 − Arm 2` 차이는 브리핑의 증분이어야 하는데
   제공자가 다르면 그 차이에 모델 차이가 섞인다. F3 가 틀린 것을 잰다.
"""

from __future__ import annotations

import json
import types

import pytest
from _fakes import FakeClient, _pack, _payload, _resp

from data import store
from decision import engine, providers


@pytest.fixture
def conn(tmp_path):
    with store.connect(tmp_path / "t.db") as c:
        store.init_db(c)
        yield c


# ── 가짜 OpenAI 클라이언트 ──────────────────────────────


def _openai_resp(payload, *, status="completed", refusal=False, reason=None):
    content = [types.SimpleNamespace(type="refusal", refusal="거부")] if refusal else []
    return types.SimpleNamespace(
        output_text=None if refusal else json.dumps(payload, ensure_ascii=False),
        output=[types.SimpleNamespace(content=content)],
        status=status,
        incomplete_details=types.SimpleNamespace(reason=reason) if reason else None,
        usage=types.SimpleNamespace(input_tokens=900, output_tokens=400),
        id="resp_test",
    )


class FakeOpenAI:
    def __init__(self, *responses):
        self.responses_given = list(responses)
        self.calls: list[dict] = []
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses_given[min(len(self.calls) - 1, len(self.responses_given) - 1)]


# ── 1. 두 제공자가 같은 계약을 낸다 ─────────────────────


def test_anthropic_응답이_Reply_로_정규화된다() -> None:
    p = providers.get("anthropic", client=FakeClient(_resp(_payload())))
    r = p.call(model="m", system="s", user="u", schema={}, params={"max_tokens": 10})
    assert isinstance(r, providers.Reply)
    assert r.stop_reason == providers.END_TURN
    assert (r.input_tokens, r.output_tokens) == (1000, 500)


def test_openai_응답이_같은_Reply_로_정규화된다() -> None:
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload())))
    r = p.call(model="m", system="s", user="u", schema={}, params={"max_tokens": 10})
    assert isinstance(r, providers.Reply)
    assert r.stop_reason == providers.END_TURN
    assert (r.input_tokens, r.output_tokens) == (900, 400)
    assert json.loads(r.raw)["market_view"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, providers.END_TURN),
        ({"status": "incomplete", "reason": "max_output_tokens"}, providers.MAX_TOKENS),
        ({"refusal": True}, providers.REFUSAL),
    ],
)
def test_openai_중단사유가_정규화된다(kwargs, expected) -> None:
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload(), **kwargs)))
    r = p.call(model="m", system="s", user="u", schema={}, params={})
    assert r.stop_reason == expected


def test_모르는_상태를_end_turn_으로_만들지_않는다() -> None:
    """`end_turn` 으로 뭉개면 잘린 응답이 정상으로 읽히고 부분 파싱 금지가 무력화된다."""
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload(), status="처음보는값")))
    r = p.call(model="m", system="s", user="u", schema={}, params={})
    assert r.stop_reason != providers.END_TURN


def test_openai_에는_스키마를_strict_로_넘긴다() -> None:
    client = FakeOpenAI(_openai_resp(_payload()))
    providers.get("openai", client=client).call(
        model="m", system="s", user="u", schema={"type": "object"}, params={}
    )
    fmt = client.calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True


def test_anthropic_전용_파라미터를_openai_에_넘기지_않는다() -> None:
    """`thinking`·`output_config` 를 그대로 넘기면 400 이다. effort 만 옮긴다."""
    client = FakeOpenAI(_openai_resp(_payload()))
    providers.get("openai", client=client).call(
        model="m",
        system="s",
        user="u",
        schema={},
        params={
            "max_tokens": 100,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        },
    )
    sent = client.calls[0]
    assert "thinking" not in sent and "output_config" not in sent
    assert sent["max_output_tokens"] == 100
    assert sent["reasoning"] == {"effort": "high"}


# ── 2. 선택과 자격증명 ──────────────────────────────────


def test_기본은_anthropic_이다(monkeypatch) -> None:
    monkeypatch.delenv("AIK_LLM_PROVIDER", raising=False)
    assert providers.get(client=FakeClient()).name == "anthropic"


def test_환경변수로_제공자를_바꾼다(monkeypatch) -> None:
    monkeypatch.setenv("AIK_LLM_PROVIDER", "openai")
    assert providers.get(client=FakeOpenAI()).name == "openai"


def test_모르는_제공자는_거부한다() -> None:
    with pytest.raises(providers.MissingCredential, match="모르는 제공자"):
        providers.get("gemini")


@pytest.mark.parametrize(
    ("name", "key"), [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")]
)
def test_자격증명은_만드는_시점에_확인한다(name, key, monkeypatch) -> None:
    """첫 호출까지 미루면 '키가 없다'가 재시도 루프 안에서 API 장애로 기록된다."""
    monkeypatch.delenv(key, raising=False)
    with pytest.raises(providers.MissingCredential, match=key):
        providers.get(name)


def test_모델은_환경변수로_고정할_수_있다(monkeypatch) -> None:
    p = providers.get("anthropic", client=FakeClient())
    monkeypatch.setenv("AIK_LLM_MODEL", "claude-sonnet-5")
    assert providers.resolve_model(p) == "claude-sonnet-5"
    assert providers.resolve_model(p, "명시값") == "명시값"  # 인자가 우선


# ── 3. 엔진과의 결합 ────────────────────────────────────


def test_제공자와_모델이_결정행에_남는다(conn) -> None:
    """교체 사실이 남지 않으면 표본을 층으로 가를 수 없다 (ADR 0007 동결 정책)."""
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload())))
    row = engine.decide(conn, _pack(), 1, provider=p, model="gpt-5.6")
    assert (row["provider"], row["model"]) == ("openai", "gpt-5.6")


def test_openai_로도_전_경로가_돈다(conn) -> None:
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload())))
    row = engine.decide(conn, _pack(), 1, provider=p)
    assert row["status"] == "ok"
    assert json.loads(row["payload"])["decisions"]


def test_openai_잘린_응답도_부분_파싱하지_않는다(conn) -> None:
    p = providers.get(
        "openai",
        client=FakeOpenAI(
            _openai_resp(_payload(), status="incomplete", reason="max_output_tokens")
        ),
    )
    with pytest.raises(engine.DecisionRefused):
        engine.decide(conn, _pack(), 1, provider=p)


def test_한_쌍_안에서_제공자가_섞이지_않는다(conn, monkeypatch) -> None:
    """arm 마다 다시 고르면 환경변수가 중간에 바뀔 때 F3 가 모델 차이를 잰다."""
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload([]))))
    monkeypatch.setenv("AIK_LLM_PROVIDER", "anthropic")  # 중간에 바뀌어도

    out = engine.decide_pair(conn, _pack(), provider=p, model="gpt-5.6")

    assert out["paired"] is True
    rows = conn.execute("SELECT DISTINCT provider, model FROM decisions").fetchall()
    assert rows == [("openai", "gpt-5.6")]  # 두 arm 이 같은 것을 썼다


def test_짝_결과에_제공자가_기록된다(conn) -> None:
    p = providers.get("anthropic", client=FakeClient(_resp(_payload([]))))
    out = engine.decide_pair(conn, _pack(), provider=p)
    assert out["provider"] == "anthropic"
    assert out["model"] == providers.AnthropicProvider.default_model


# ── 캐시 적중 ───────────────────────────────────────────


def test_openai_캐시된_입력을_싣는다() -> None:
    """비용의 대부분이 입력이고, 캐시된 몫은 정가의 1/10 이다.
    **재지 않으면 같은 토큰 수가 열 배까지 차이난다.**
    """
    r = _openai_resp(_payload())
    r.usage.input_tokens_details = types.SimpleNamespace(cached_tokens=700)
    p = providers.get("openai", client=FakeOpenAI(r))
    got = p.call(model="m", system="s", user="u", schema={}, params={})
    assert got.cached_input_tokens == 700
    # OpenAI 는 캐시된 몫을 input_tokens 에 **포함해서** 보고한다
    assert got.input_tokens == 900
    assert got.cache_hit_pct == pytest.approx(77.8)


def test_제공자가_안_주면_0_이_아니라_모른다() -> None:
    """0 으로 두면 *캐시가 안 먹었다* 와 *재지 못했다* 가 구분되지 않고,
    없는 개선 여지를 만들어낸다."""
    p = providers.get("openai", client=FakeOpenAI(_openai_resp(_payload())))
    got = p.call(model="m", system="s", user="u", schema={}, params={})
    assert got.cached_input_tokens is None
    assert got.cache_hit_pct is None


def test_anthropic_은_캐시_읽기를_따로_준다() -> None:
    """Anthropic 은 `cache_read_input_tokens` 를 input_tokens 와 **별도로** 준다."""
    r = _resp(_payload())
    r.usage.cache_read_input_tokens = 500
    p = providers.get("anthropic", client=FakeClient(r))
    got = p.call(model="m", system="s", user="u", schema={}, params={"max_tokens": 10})
    assert got.cached_input_tokens == 500


def test_캐시_토큰이_결정행에_남는다(conn) -> None:
    """Reply 까지만 오고 DB 에 안 남으면 **어제 적중률을 물을 수 없다.**"""
    r = _openai_resp(_payload())
    r.usage.input_tokens_details = types.SimpleNamespace(cached_tokens=600)
    prov = providers.get("openai", client=FakeOpenAI(r))
    row = engine.decide(conn, _pack(), 1, provider=prov)
    got = conn.execute(
        "SELECT cached_input_tokens FROM decisions WHERE decision_id=?", (row["decision_id"],)
    ).fetchone()
    assert got[0] == 600
