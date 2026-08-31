"""집행 모드와 킬 스위치. **기본값은 언제나 가장 안전한 쪽이다.**

## 왜 파일 킬 스위치가 필요한가

환경변수는 **프로세스 시작 시 고정된다.** `KILL_SWITCH=true` 로 `.env` 를 고쳐도
이미 돌고 있는 배치는 멈추지 않는다 — 정작 멈추고 싶은 순간에 안 듣는다.
그래서 **파일 존재**로도 켤 수 있게 한다. 둘 중 **하나라도** 켜져 있으면 차단이다.

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


def kiwoom_env() -> str:
    """키움 서버. `real` 또는 `mock`. 없으면 `mock` 으로 본다."""
    raw = (os.getenv("KIWOOM_ENV") or "").strip().lower()
    return "real" if raw == "real" else "mock"


def check_coherent() -> list[str]:
    """모드와 서버가 어긋나면 잡는다. **여기가 가장 중요한 검사다.**

    실측(2026-09-01): `.env` 가 `EXECUTION_MODE=paper` 인데 `KIWOOM_ENV=real` 이었다.
    조회만 하던 동안은 무해했지만, **주문 코드가 생기는 순간 실전 서버로 주문이 간다.**
    모의투자를 한다면서 실계좌를 치는 것이 이 한 줄 차이다.
    """
    problems = []
    m, env = mode(), kiwoom_env()
    if m == MOCK and env == "real":
        problems.append(
            f"EXECUTION_MODE={m} 인데 KIWOOM_ENV={env} 다. "
            "모의투자를 한다면서 실전 서버로 주문이 나간다 — KIWOOM_ENV=mock 으로 고쳐라"
        )
    if m == LIVE and env != "real":
        problems.append(
            f"EXECUTION_MODE={m} 인데 KIWOOM_ENV={env} 다. "
            "실계좌 모드인데 모의 서버를 향한다 — 둘 중 하나가 틀렸다"
        )
    if m == LIVE and (os.getenv("AIK_LIVE_ACK") or "").strip() != "I_UNDERSTAND":
        problems.append(
            "EXECUTION_MODE=live 인데 AIK_LIVE_ACK 승인이 없다. "
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
    "kiwoom_env",
    "mode",
]
