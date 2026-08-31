"""집행 모드와 킬 스위치. **기본값은 언제나 가장 안전한 쪽이다.**

## 왜 파일 킬 스위치가 필요한가

환경변수는 **프로세스 시작 시 고정된다.** `KILL_SWITCH=true` 로 `.env` 를 고쳐도
이미 돌고 있는 배치는 멈추지 않는다 — 정작 멈추고 싶은 순간에 안 듣는다.
그래서 **파일 존재**로도 켤 수 있게 한다. 둘 중 **하나라도** 켜져 있으면 차단이다.

## 문장에도 위험 설정을 리터럴로 쓰지 않는다

`repo-guard` 는 **설정과 산문을 구분하지 못한다.** 일부러 무디게 만든 것이고, 그래서
주석·독스트링에 `EXECUTION_MODE` 를 `live` 로 적는 식의 리터럴을 쓰면 CI 가 막는다.
실제로 이 파일이 그걸로 한 번 막혔다(2026-09-01). **가드를 느슨하게 하지 말고 문장을 고친다.**

## 라벨이 아니라 URL 이 진실이다

`KIWOOM_ENV` 를 `real` 로 두는 것 같은 **환경 라벨은 아무것도 강제하지 않는다.** 실측(2026-09-01):
`.env` 에 그 값이 있었지만 **읽는 코드가 없었고**, 실제 서버는 `KIWOOM_REST_BASE` URL 이
정하고 있었다. 라벨을 보고 판정하면 게이트가 **거짓 안심**을 준다.

그래서 **주문이 실제로 향할 URL** 을 본다. 그리고 조회용과 주문용을 나눈다 —
조회는 실전이 낫고(유량 3.5배), 주문은 반드시 모의여야 하기 때문이다.

## 읽을 수 없으면 켜진 것으로 본다

값이 이상하면 "꺼짐"이 아니라 **"켜짐"** 으로 판정한다. 이 저장소가 반복해 당한 실패 방식이
*"동작하지 않는 것과 동작해서 통과하는 것은 겉으로 구분되지 않는다"* 이고,
킬 스위치에서 그 실수는 되돌릴 수 없다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from data import config as dcfg

# 모드. 뒤로 갈수록 위험하다.
PAPER, MOCK, LIVE = "paper", "mock", "live"
MODES = (PAPER, MOCK, LIVE)

# 이 파일이 있으면 킬 스위치가 켜진 것이다. 돌고 있는 프로세스도 다음 판정에서 멈춘다.
KILL_FILE = Path(os.getenv("AIK_KILL_FILE", str(dcfg.DATA_DIR / "KILL")))

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class GateConfigError(RuntimeError):
    """설정이 모순이다. **주문을 내면 안 되는 상태**이므로 예외로 세운다."""


@dataclass(frozen=True)
class KillState:
    on: bool
    reason: str


def kill_switch() -> KillState:
    """켜져 있는가. **읽을 수 없으면 켜진 것으로 본다.**"""
    if KILL_FILE.exists():
        return KillState(True, f"킬 파일 존재: {KILL_FILE}")
    raw = (os.getenv("KILL_SWITCH") or "").strip().lower()
    if raw in _TRUE:
        return KillState(True, "KILL_SWITCH 환경변수가 켜져 있다")
    if raw in _FALSE:
        return KillState(False, "")
    return KillState(True, f"KILL_SWITCH={raw!r} 를 해석할 수 없다 — 켜진 것으로 본다")


def mode() -> str:
    """집행 모드. **없거나 모르는 값이면 `paper`** — 주문이 나가지 않는 쪽이다."""
    raw = (os.getenv("EXECUTION_MODE") or "").strip().lower()
    return raw if raw in MODES else PAPER


def _is_mock_host(url: str) -> bool:
    """모의 서버인가. **`data/sources/kiwoom.py` 와 같은 판정을 쓴다.**

    두 곳이 다르게 판정하면 한쪽이 "모의"라고 믿는 동안 다른 쪽이 실전을 친다.
    """
    from data.sources.kiwoom import MOCK_HOST_MARK

    return MOCK_HOST_MARK in (url or "")


def read_base() -> str:
    """조회용 엔드포인트. 일봉·현재가·분봉이 여기로 간다."""
    return (os.getenv("KIWOOM_REST_BASE") or "").strip().rstrip("/")


def order_base() -> str:
    """**주문용 엔드포인트. 조회와 분리한다.**

    조회는 실전 서버가 낫다 — 데이터는 같은데 유량이 3.5배 관대하다(실측: 실전 동시 10~11
    vs 모의 동시 3·초당 2회). 그런데 **주문까지 같은 값을 쓰면 조회를 빠르게 하려다
    주문이 실계좌로 간다.** 그래서 변수를 나눈다.

    비어 있으면 빈 문자열이다 — **조회용으로 조용히 대체하지 않는다.** 대체하는 순간
    이 분리가 무의미해진다.
    """
    return (os.getenv("KIWOOM_ORDER_BASE") or "").strip().rstrip("/")


def order_target() -> str:
    """주문이 실제로 향하는 곳. `mock` / `real` / `unset`.

    **환경 라벨(`KIWOOM_ENV`)을 믿지 않는다.** 실측(2026-09-01): `.env` 에
    `KIWOOM_ENV` 가 `real` 이었지만 **읽는 코드가 없었다** — 실제 서버는
    `KIWOOM_REST_BASE` URL 이 정하고 있었다. 라벨을 보고 판정하면 거짓 안심을 준다.
    """
    b = order_base()
    if not b:
        return "unset"
    return "mock" if _is_mock_host(b) else "real"


def check_coherent() -> list[str]:
    """모드와 **주문이 실제로 향하는 URL** 이 어긋나면 잡는다. 가장 중요한 검사다.

    `paper` 는 주문을 내지 않으므로 URL 을 요구하지 않는다.
    """
    problems = []
    m, target = mode(), order_target()

    if m in (MOCK, LIVE) and target == "unset":
        problems.append(
            f"EXECUTION_MODE={m} 인데 KIWOOM_ORDER_BASE 가 비어 있다. "
            "주문이 어디로 갈지 정해지지 않았다 — 조회용(KIWOOM_REST_BASE)으로 대체하지 않는다"
        )
    if m == MOCK and target == "real":
        problems.append(
            f"EXECUTION_MODE={m} 인데 주문 엔드포인트가 실전이다({order_base()}). "
            "모의투자를 한다면서 실계좌로 주문이 나간다"
        )
    if m == LIVE and target == "mock":
        problems.append(
            f"EXECUTION_MODE={m} 인데 주문 엔드포인트가 모의다({order_base()}). "
            "실계좌 모드인데 모의 서버를 향한다 — 둘 중 하나가 틀렸다"
        )
    if m == LIVE and (os.getenv("AIK_LIVE_ACK") or "").strip() != "I_UNDERSTAND":
        problems.append(
            f"EXECUTION_MODE={LIVE} 인데 AIK_LIVE_ACK 승인이 없다. "
            "실계좌는 사람이 명시적으로 확인해야 한다"
        )
    return problems


__all__ = [
    "LIVE",
    "MOCK",
    "MODES",
    "PAPER",
    "GateConfigError",
    "KillState",
    "check_coherent",
    "kill_switch",
    "mode",
    "order_base",
    "order_target",
    "read_base",
]
