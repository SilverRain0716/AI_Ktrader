"""테스트 공용 가짜 객체 — 실제 API 를 부르지 않고 응답의 모든 모양을 만든다.

`test_` 로 시작하지 않으므로 pytest 가 테스트로 수집하지 않는다.
`test_engine.py` 와 `test_providers.py` 가 같은 팩·같은 결정을 써야
두 파일의 결과를 나란히 읽을 수 있다.
"""

from __future__ import annotations

import json
import types


def _pack() -> dict:
    return {
        "pack_id": "20260830-0929-premarket",
        "cycle": "premarket",
        "generated_at": "2026-08-30T09:29:00+09:00",
        "market": {"session": "PRE"},
        "account": {"total_equity_krw": 100_000_000, "is_mock": True},
        "positions": [{"code": "000660", "sector": "반도체", "weight_pct": 10.0}],
        "universe": [
            {
                "code": "005930",
                "name": "삼성전자",
                "sector": "반도체",
                "indicators": {"close": 70000, "adv20_eok_krw": 5000.0},
                "screen_reasons": ["momentum: 정배열"],
                "channels": ["momentum"],
            },
            {
                "code": "035720",
                "name": "카카오",
                "sector": "인터넷",
                "indicators": {"close": 50000, "adv20_eok_krw": 3000.0},
                "screen_reasons": ["briefing:kr-close-deep 주목"],
                "channels": ["briefing"],
            },
            {
                "code": "000270",
                "name": "기아",
                "sector": "자동차",
                "indicators": {"close": 90000, "adv20_eok_krw": 2000.0},
                "screen_reasons": ["briefing:us-close 주목", "flow: 기관 3일"],
                "channels": ["briefing", "flow"],
            },
        ],
        "briefings": [{"briefing_id": "b1", "kind": "kr-close-deep", "views": []}],
        "constraints": {
            "max_positions": 8,
            "max_new_entries_this_cycle": 2,
            "max_weight_pct_per_name": 15.0,
            "max_weight_pct_per_sector": 35.0,
            "max_order_vs_adv_pct": 5.0,
            "daily_loss_limit_hit": False,
            "blocked_codes": [],
        },
        "data_quality": {
            "ohlcv_as_of": "2026-08-20",
            "warnings": ["브리핑 결손: us-close", "수급 데이터 없음"],
        },
    }


def _decision(code="005930", **over) -> dict:
    d = {
        "action": "BUY",
        "code": code,
        "name": None,
        "weight_pct": 10.0,
        "rank": 1,
        "entry": {"type": "MARKET", "price": None, "condition": None, "valid_until": None},
        "stop": {"type": "ATR", "value": 2.0},
        "target": None,
        "trail": None,
        "max_hold_days": 10,
        "confidence": "중",
        "reasons": ["정배열 RSI 65", "거래대금 5,000억"],
        "invalidation": {
            "type": "close_below_ma",
            "value": 20,
            "deadline": None,
            "text": "20일선 이탈",
        },
        "briefing_refs": [],
        "sources": [],
    }
    d.update(over)
    return d


def _hold(code: str) -> dict:
    """보유 종목을 유지한다는 결정. 진입 관련 필드는 전부 null 이다."""
    return _decision(
        action="HOLD",
        code=code,
        rank=None,
        weight_pct=None,
        entry=None,
        stop=None,
        max_hold_days=None,
    )


def _payload(decisions=None, **over) -> dict:
    p = {
        "market_view": "코스피 20일선 위.",
        "abstain": False,
        "abstain_reason": None,
        # 기본 payload 는 **보유 종목(000660)을 언급한다.** `_pack()` 이 그것을 들고 있고,
        # 보유를 빠뜨린 결정은 이제 거부되기 때문이다 — 픽스처가 그 규칙을 어기면
        # 모든 테스트가 "규칙 없는 세계"를 검사하게 된다.
        # 기본값에 `_hold("000660")` 을 넣지 않는다. `decide(conn=...)` 는 **DB 에서**
        # 보유를 다시 읽는데(swap_account) 테스트 DB 는 비어 있어서, 픽스처가 들고 있다고
        # 주장하면 "보유 종목이 아니다" 로 거부된다. 팩만 넘기는 `validate()` 테스트는
        # `_hold()` 를 직접 붙인다.
        "decisions": decisions if decisions is not None else [_decision()],
        "portfolio_note": None,
        "data_concerns": [],
    }
    p.update(over)
    return p


class FakeClient:
    """응답을 미리 정해두고 순서대로 내준다. 부족하면 마지막 것을 반복한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _resp(payload, *, stop_reason="end_turn", raw=None):
    text = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=1000, output_tokens=500),
        _request_id="req_test",
    )
