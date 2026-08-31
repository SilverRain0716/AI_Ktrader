"""테스트 공통 픽스처.

리스크 한도는 기본값이 없다(ADR 0004). 테스트도 예외가 아니므로 여기서 주입한다.
값을 한 곳에 모아두면 "테스트가 무슨 한도로 돌았는가"를 나중에 추적할 수 있다.

**그리고 외부 자격증명을 지운다.** `.env` 가 로드된 환경에서 테스트를 돌리면 가짜 제공자
대신 실제 클라이언트가 만들어져 **테스트가 유료 API 를 친다.** 실제로 그랬다 —
`OPENAI_API_KEY` 를 `.env` 에 넣은 순간 `test_engine.py` 40건 중 17건이 깨졌고
실행 시간이 3초에서 73초로 늘었다(2026-09-01).

**CI 에서는 키가 없어 초록불이었다.** 로컬에만 나타나는 종류의 결함이고, 그래서
CI 를 믿고 넘어가면 안 되는 자리다.
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


# 테스트가 절대 들고 있으면 안 되는 것. 지우지 않으면 실제 호출이 나간다.
# 이 키가 필요한 테스트는 `net` 마커를 달고 스스로 monkeypatch.setenv 한다.
_EXTERNAL_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AIK_LLM_PROVIDER",
    "AIK_LLM_MODEL",
)


def test_외부_자격증명_목록이_비어_있지_않다():
    """목록을 비우면 이 파일이 아무 일도 안 하게 된다 — 그것이 조용히 지나간다."""
    assert _EXTERNAL_CREDENTIALS


@pytest.fixture(autouse=True)
def _no_external_credentials(monkeypatch):
    """실제 API 키를 지운다. **가짜 제공자가 진짜로 쓰이도록 강제한다.**"""
    for name in _EXTERNAL_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _risk_limits(monkeypatch):
    """모든 테스트에 한도를 주입한다.

    한도를 지우고 싶은 테스트는 monkeypatch.delenv 로 개별적으로 지운다 —
    autouse 픽스처가 monkeypatch 를 쓰므로 테스트가 끝나면 자동으로 복원된다.
    """
    for name, value in TEST_LIMITS.items():
        monkeypatch.setenv(name, value)
