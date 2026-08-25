"""테스트 공통 픽스처.

리스크 한도는 기본값이 없다(ADR 0004). 테스트도 예외가 아니므로 여기서 주입한다.
값을 한 곳에 모아두면 "테스트가 무슨 한도로 돌았는가"를 나중에 추적할 수 있다.
"""

from __future__ import annotations

import pytest

# 실제 운용 값이 아니다. 테스트 전용 고정값이다.
TEST_LIMITS = {
    "AIK_MAX_POSITIONS": "8",
    "AIK_MAX_NEW_ENTRIES_PER_CYCLE": "2",
    "AIK_MAX_WEIGHT_PCT_PER_NAME": "12.0",
    "AIK_MAX_WEIGHT_PCT_PER_SECTOR": "30.0",
    "AIK_MAX_RISK_PCT_PER_TRADE": "0.5",
    "AIK_DAILY_LOSS_LIMIT_KRW": "0",
    "AIK_MAX_ORDER_VS_ADV_PCT": "3.0",
    "AIK_PAPER_EQUITY_KRW": "100000000",
}


def test_픽스처가_모든_한도를_덮는다():
    """항목을 추가하고 여기를 안 고치면, 다른 테스트가 알 수 없는 KeyError 로 죽는다.

    (pytest 는 conftest 의 test_ 함수도 수집한다 — 실패가 바로 여기를 가리키게 둔다.)
    """
    from decision import config

    assert set(TEST_LIMITS) == set(config._LIMIT_SPECS)


@pytest.fixture(autouse=True)
def _risk_limits(monkeypatch):
    """모든 테스트에 한도를 주입한다.

    한도를 지우고 싶은 테스트는 monkeypatch.delenv 로 개별적으로 지운다 —
    autouse 픽스처가 monkeypatch 를 쓰므로 테스트가 끝나면 자동으로 복원된다.
    """
    for name, value in TEST_LIMITS.items():
        monkeypatch.setenv(name, value)
