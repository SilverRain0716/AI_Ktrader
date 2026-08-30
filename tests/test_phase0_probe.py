"""Phase 0 실측 도구의 순수 함수 검사.

프로브 자체는 네트워크를 타므로 여기서 돌리지 않는다. 다만 **파싱은 순수 함수**이고,
여기가 틀리면 실측값이 조용히 뒤집힌다 — 그 경로만 막는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# 정본은 적재 경로에 있다. 프로브가 그것을 재수출하는지도 함께 확인한다.

from data.sources.kiwoom import kiwoom_price


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-257000", 257000),  # 하락 표시. 음수가 아니다
        ("+257000", 257000),  # 상승 표시
        ("257000", 257000),  # 보합·무표시
        (257000, 257000),  # 이미 정수인 경우
        (" -257000 ", 257000),  # 공백 섞임
        ("0", 0),
        ("-0", 0),
        ("", None),
        (None, None),
    ],
)
def test_부호_접두는_가격이_아니라_등락표시다(raw, expected) -> None:
    assert kiwoom_price(raw) == expected


def test_부호를_떼지_않으면_가격이_음수가_된다() -> None:
    """이 테스트가 지키는 것: int() 를 직접 부르는 회귀."""
    assert int("-257000") < 0  # 이렇게 파싱하면 틀린다
    assert kiwoom_price("-257000") > 0  # 이렇게 파싱해야 한다


def test_프로브에_사본이_남아_있지_않다() -> None:
    """같은 함수가 두 곳에 있으면 한쪽만 고쳐지고 그 사실이 드러나지 않는다."""
    import phase0_probe

    assert not hasattr(phase0_probe, "kiwoom_price")
