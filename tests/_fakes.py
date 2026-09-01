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


def _payload(decisions=None, **over) -> dict:
    p = {
        "market_view": "코스피 20일선 위.",
        "abstain": False,
        "abstain_reason": None,
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
