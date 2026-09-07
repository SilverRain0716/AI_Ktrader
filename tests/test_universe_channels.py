"""채널 구성 — **측정 경로와 운영 경로가 같은 코드를 쓴다.**

스크립트가 `build` 를 베껴 쓰면 측정이 운영을 설명하지 못한다.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from data import store
from decision import config as ccfg
from decision import universe as U


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    return conn


def test_채널을_고를_수_있다(db, monkeypatch):
    """`channels` 를 주면 그 채널만 돈다 — 없으면 채널 제거 측정을 할 수 없다."""
    called: list[str] = []
    for name in ("_briefing_channel", "_momentum_channel", "_flow_channel"):
        monkeypatch.setattr(
            U, name, lambda *a, _n=name, **k: (called.append(_n), [])[1], raising=True
        )
    monkeypatch.setattr(U, "hard_filter", lambda *a, **k: {})

    U.build(db, date(2026, 9, 4), channels=("flow",))
    assert called == ["_flow_channel"]

    called.clear()
    U.build(db, date(2026, 9, 4))
    assert set(called) == {"_briefing_channel", "_momentum_channel", "_flow_channel"}


def test_정원도_바꿀_수_있다(db, monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(U, "hard_filter", lambda *a, **k: {})
    monkeypatch.setattr(U, "_flow_channel", lambda _pool, q: (seen.append(q), [])[1])
    monkeypatch.setattr(U, "_briefing_channel", lambda *a, **k: [])
    monkeypatch.setattr(U, "_momentum_channel", lambda *a, **k: [])

    U.build(db, date(2026, 9, 4), channels=("flow",), quota={"flow": 7})
    assert seen == [7]


def test_운영_기본값은_바뀌지_않는다(db, monkeypatch):
    """측정용 인자를 안 주면 **설정 파일의 정원**을 그대로 쓴다."""
    seen: dict[str, int] = {}
    monkeypatch.setattr(U, "hard_filter", lambda *a, **k: {})
    monkeypatch.setattr(
        U, "_briefing_channel", lambda _c, _p, _a, q, _n: (seen.setdefault("briefing", q), [])[1]
    )
    monkeypatch.setattr(
        U, "_momentum_channel", lambda _p, q: (seen.setdefault("momentum", q), [])[1]
    )
    monkeypatch.setattr(U, "_flow_channel", lambda _p, q: (seen.setdefault("flow", q), [])[1])

    U.build(db, date(2026, 9, 4))
    assert seen == dict(ccfg.CHANNEL_QUOTA)


def test_momentum_정원이_flow_보다_작다():
    """실측 60거래일에서 momentum 이 세 구간 모두 모집단보다 나빴다 (+20일 -7.3%p).
    **0 으로 두지 않는 이유는 측정 구간이 하락장 하나뿐이기 때문이다** —
    빼버리면 상승장에서 어떤지 영원히 못 잰다.
    """
    assert ccfg.CHANNEL_QUOTA["momentum"] < ccfg.CHANNEL_QUOTA["flow"]
    assert ccfg.CHANNEL_QUOTA["momentum"] > 0


def test_브리핑_기간이_한_주_이상이다():
    """3일이면 하루 2~3종목뿐이라 **+20일 표본이 14건**에서 늘지 않았다.
    성능이 아니라 표본을 만들려고 늘린 값이다.
    """
    assert ccfg.BRIEFING_LOOKBACK_DAYS >= 7
