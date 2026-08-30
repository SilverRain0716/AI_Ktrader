"""장중 현재가 — 못 받았을 때 받은 척하지 않는가 (ADR 0009 결정 8).

12:20·15:00 사이클이 전 거래일 종가만 보고 있었다. 설계안 v1 4.3 이 12:20 에 요구한 것은
"오전 흐름 반영"인데 구현이 그것을 할 수 없었다.

여기서 지키는 것은 "현재가를 가져온다"가 아니라 **"못 가져왔을 때 그 사실이 드러난다"** 이다.
전 거래일 종가를 장중 가격인 것처럼 넘기면 AI 는 오전을 봤다고 착각한 채 판단한다 —
이 저장소가 반복해 당한 실패 방식 그대로다.
"""

from __future__ import annotations

import types

import pytest

from data.sources import kiwoom

# ── 1. 가격 파싱과 자체 대조 ────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("-257000", 257000), ("+257000", 257000), ("257000", 257000), ("", None), (None, None)],
)
def test_부호_접두를_뗀다(raw, expected) -> None:
    assert kiwoom.kiwoom_price(raw) == expected


def test_거래소_등락률과_대조한다() -> None:
    """실측(2026-08-30 삼성전자): (257000−266000)/266000 = −3.38% = flu_rt."""
    q = kiwoom.SpotQuote("005930", 257000, 266000, -3.38, "2026-08-30T12:20:00+09:00")
    assert q.change_pct == -3.38
    assert q.consistent


def test_부호를_안_떼면_대조에_걸린다() -> None:
    """이 검사가 사는지를 본다 — 통과만 보면 대조기가 죽어 있어도 초록불이다."""
    broken = kiwoom.SpotQuote("005930", -257000, 266000, -3.38, "x")
    assert not broken.consistent


def test_대조할_것이_없으면_판정하지_않는다() -> None:
    assert kiwoom.SpotQuote("005930", 257000, None, None, "x").consistent


# ── 2. 클라이언트 ───────────────────────────────────────


def _resp(payload, status=200):
    return types.SimpleNamespace(status_code=status, json=lambda: payload)


class FakeHTTP:
    """경로별로 응답을 정해둔다."""

    def __init__(self, quotes: dict, *, token="tok", fail: set | None = None):
        self.quotes, self.token_val, self.fail = quotes, token, fail or set()
        self.calls: list[str] = []

    def post(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/oauth2/token"):
            if self.token_val is None:
                return _resp({"return_code": 3, "return_msg": "인증 실패"})
            return _resp({"token": self.token_val, "expires_dt": "20270101000000"})
        code = kw["json"]["stk_cd"]
        if code in self.fail:
            return _resp({"return_code": 2, "return_msg": "조회 실패"})
        return _resp({"return_code": 0, **self.quotes[code]})

    def close(self):
        pass


def _client(quotes, **kw):
    return kiwoom.KiwoomClient(base="https://example.test", http=FakeHTTP(quotes, **kw))


QUOTES = {
    "005930": {"cur_prc": "-257000", "base_pric": "266000", "flu_rt": "-3.38"},
    "000660": {"cur_prc": "+120000", "base_pric": "110000", "flu_rt": "9.09"},
}


def test_현재가를_가져온다() -> None:
    q = _client(QUOTES).spot("005930")
    assert (q.price, q.prev_close, q.change_pct) == (257000, 266000, -3.38)


def test_등락률이_어긋나면_값을_쓰지_않는다() -> None:
    """틀린 가격은 없는 가격보다 나쁘다."""
    bad = {"005930": {"cur_prc": "-257000", "base_pric": "266000", "flu_rt": "+99.0"}}
    with pytest.raises(kiwoom.KiwoomUnavailable, match="등락률 불일치"):
        _client(bad).spot("005930")


def test_토큰을_재사용한다() -> None:
    c = _client(QUOTES)
    c.spot("005930")
    c.spot("000660")
    assert sum(1 for u in c._http.calls if u.endswith("/oauth2/token")) == 1


def test_자격증명이_없으면_분명하게_멈춘다(monkeypatch) -> None:
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    with pytest.raises(kiwoom.KiwoomUnavailable, match="KIWOOM_APP_KEY"):
        kiwoom.KiwoomClient(base="https://example.test", http=FakeHTTP({})).token()


def test_실패한_종목을_조용히_빼지_않는다(monkeypatch) -> None:
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    ok, failed = _client(QUOTES, fail={"000660"}).spots(["005930", "000660"])
    assert set(ok) == {"005930"}
    assert list(failed) == ["000660"]
    assert "조회 실패" in failed["000660"]  # 사유가 남는다


# ── 3. 팩 배선 — 여기가 핵심이다 ────────────────────────


def _pack_stub() -> dict:
    return {
        "universe": [
            {"code": "005930", "indicators": {"close": 260000, "change_pct": 1.0}},
            {"code": "000660", "indicators": {"close": 111000, "change_pct": 0.5}},
        ],
        "positions": [{"code": "005930", "current_price": 260000}],
        "data_quality": {"warnings": []},
    }


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    monkeypatch.setenv("KIWOOM_REST_BASE", "https://example.test")


@pytest.mark.parametrize("cycle", ["midday", "preclose", "event"])
def test_장중_사이클은_현재가로_덮는다(cycle) -> None:
    from decision import pack as packmod

    p = _pack_stub()
    packmod._overlay_spot(p, cycle, client=_client(QUOTES))

    assert p["data_quality"]["price_source"] == "intraday"
    assert p["data_quality"]["price_as_of"]
    assert p["universe"][0]["indicators"]["close"] == 257000
    assert p["universe"][0]["indicators"]["change_pct"] == -3.38
    assert p["positions"][0]["current_price"] == 257000


@pytest.mark.parametrize("cycle", ["premarket", "postmarket"])
def test_장_밖_사이클은_전일_종가가_맞는_값이다(cycle) -> None:
    """개장 전에는 전일 종가가 오류가 아니라 정답이다. 부르지도 않는다."""
    from decision import pack as packmod

    p = _pack_stub()
    http = FakeHTTP(QUOTES)
    packmod._overlay_spot(p, cycle, client=kiwoom.KiwoomClient(base="https://x", http=http))

    assert p["data_quality"]["price_source"] == "daily_close"
    assert p["universe"][0]["indicators"]["close"] == 260000  # 그대로
    assert http.calls == []  # 호출조차 하지 않는다


def test_현재가를_못_받으면_경고가_남는다() -> None:
    """**받은 척하지 않는다.** 이것이 이 파일의 존재 이유다."""
    from decision import pack as packmod

    p = _pack_stub()
    packmod._overlay_spot(
        p, "midday", client=kiwoom.KiwoomClient(base="https://x", http=FakeHTTP({}, token=None))
    )

    dq = p["data_quality"]
    assert dq["price_source"] == "daily_close"
    assert any("전 거래일 종가" in w for w in dq["warnings"])
    assert any("오전 흐름" in w for w in dq["warnings"])
    assert p["universe"][0]["indicators"]["close"] == 260000  # 덮이지 않았다


def test_일부만_받으면_섞지_않고_통째로_전일_종가로_간다() -> None:
    """20종목 중 3종목만 전일 종가면 그 3종목의 등락률이 다른 것을 재게 되고,
    그 사실이 겉으로 보이지 않는다. 섞느니 통일하는 편이 정직하다."""
    from decision import pack as packmod

    p = _pack_stub()
    packmod._overlay_spot(p, "midday", client=_client(QUOTES, fail={"000660"}))

    dq = p["data_quality"]
    assert dq["price_source"] == "daily_close"
    assert any("결손" in w and "섞지 않고" in w for w in dq["warnings"])
    # 성공한 종목도 덮이지 않았다 — 통일이 요점이다
    assert p["universe"][0]["indicators"]["close"] == 260000


def test_유니버스가_비면_부르지_않는다() -> None:
    from decision import pack as packmod

    p = {"universe": [], "positions": [], "data_quality": {"warnings": []}}
    http = FakeHTTP({})
    packmod._overlay_spot(p, "midday", client=kiwoom.KiwoomClient(base="https://x", http=http))
    assert p["data_quality"]["price_source"] == "daily_close"
    assert http.calls == []


# ── 4. 실전과 모의는 유량이 다르다 ──────────────────────


@pytest.mark.parametrize(
    ("base", "is_mock", "workers"),
    [
        ("https://api.kiwoom.com", False, kiwoom.MAX_CONCURRENCY),
        ("https://mockapi.kiwoom.com", True, kiwoom.MOCK_MAX_CONCURRENCY),
    ],
)
def test_모의는_동시성을_낮춘다(base, is_mock, workers) -> None:
    """실측(2026-08-30): 실전 동시 통과 10~11 · 모의 3 고정(지속은 2콜/초 상한).

    실전 기준 동시성을 모의에 그대로 쓰면 8건 중 5~6건이 429 다.
    """
    c = kiwoom.KiwoomClient(base=base, http=FakeHTTP({}))
    assert c.is_mock is is_mock
    assert c.max_workers == workers


def test_모의_동시성이_실전보다_낮다() -> None:
    """상수를 뒤집어 놓으면 이 테스트가 잡는다 — 값만 보면 어느 쪽이 어느 쪽인지 모른다."""
    assert kiwoom.MOCK_MAX_CONCURRENCY < kiwoom.MAX_CONCURRENCY


def test_종목_수보다_많은_워커를_만들지_않는다() -> None:
    c = kiwoom.KiwoomClient(base="https://api.kiwoom.com", http=FakeHTTP(QUOTES))
    ok, failed = c.spots(["005930"])
    assert set(ok) == {"005930"} and not failed


def test_엔드포인트로_판정한다_환경변수가_아니라(monkeypatch) -> None:
    """KIWOOM_ENV 는 읽는 코드가 따로 없어 믿을 수 없다. 실제로 어디에 쏘는지는 base 가 정한다."""
    monkeypatch.setenv("KIWOOM_ENV", "real")
    assert kiwoom.KiwoomClient(base="https://mockapi.kiwoom.com", http=FakeHTTP({})).is_mock


def test_모의는_호출_간격도_강제한다() -> None:
    """동시성만 낮추면 부족하다 — 동시 2 라도 RTT 0.7초면 초당 2.8회가 나간다.
    실제로 그렇게 해서 20종목 중 1종목이 429 였다. 제약은 동시성이 아니라 속도다."""
    real = kiwoom.KiwoomClient(base="https://api.kiwoom.com", http=FakeHTTP({}))
    mock = kiwoom.KiwoomClient(base="https://mockapi.kiwoom.com", http=FakeHTTP({}))
    assert real.min_interval == 0.0
    assert mock.min_interval > 0
    assert 1 / mock.min_interval <= 2.0  # 모의 지속 상한 2콜/초를 넘지 않는다


def test_페이싱이_실제로_기다린다() -> None:
    import time as _t

    c = kiwoom.KiwoomClient(base="https://mockapi.kiwoom.com", http=FakeHTTP(QUOTES))
    t0 = _t.monotonic()
    c.spot("005930")
    c.spot("005930")
    assert _t.monotonic() - t0 >= kiwoom.MOCK_MIN_INTERVAL_SEC
