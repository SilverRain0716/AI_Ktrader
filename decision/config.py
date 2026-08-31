"""판단 계층 설정.

숫자는 전부 여기에 모은다. 흩어지면 "왜 이 값인가"를 추적할 수 없다.
운용 자금·리스크 한도는 저장소에 커밋하지 않고 환경변수로만 받는다 (ADR 0004).
"""

from __future__ import annotations

import os
import re

# .env 를 읽는다. 이 import 가 없으면 ".env.example 을 .env 로 복사하라"는 안내가 거짓말이 된다
# — 기본값이 있던 시절에는 무해했지만, 기본값을 없앤 지금은 유일한 경로가 막힌다.
from data import env as _env  # noqa: F401  (import 부수효과가 목적이다)

# ── 유니버스 ────────────────────────────────────────────
# 하드 필터: 통과 못하면 무조건 제외. 슬리피지가 전략을 삼키는 구간을 잘라낸다.
MIN_ADV20_EOK_KRW = 100.0  # 20일 평균 거래대금 하한 (억원)
MIN_MARKET_CAP_EOK_KRW = 3000.0  # 시가총액 하한 (억원)
MIN_BARS = 120  # 지표 계산에 필요한 최소 유효봉
# 증거금률 상한 (ADR 0013 원칙 1). 이 값 이하 등급만 유니버스에 들어온다.
# **시총으로 대신할 수 없다** — 실측(2026-08-31, 전 종목 대조): 시총 3,000억 기준의
# 정밀도가 90.1% 라 10종목 중 1개가 증50~100% 다. 삼천당제약은 시총 4조에 증100% 다.
MARGIN_MAX_PCT = 40
HALT_LOOKBACK_DAYS = 20  # 최근 이 기간에 거래정지 흔적이 있으면 제외

# 3채널 랭킹. 단일 점수로 줄을 세우면 한 가지 성격의 종목만 상위를 채운다.
CHANNEL_QUOTA = {"briefing": 20, "momentum": 20, "flow": 20}
UNIVERSE_MAX = 60

# 채널별 조건
BRIEFING_LOOKBACK_DAYS = 3
BRIEFING_STANCES = ("주목", "조건부")
MOMENTUM_RSI_RANGE = (50.0, 70.0)
FLOW_MIN_NET_DAYS = 3

# 유니버스가 이보다 적으면 스크리닝이 깨진 것으로 보고 팩 생성을 거부한다.
MIN_UNIVERSE_SIZE = 5
# 하드 필터 통과 종목이 이보다 적으면 랭킹이 무의미하다(어차피 전부 통과). 경고를 남긴다.
# 주의: 이 숫자만으로는 아무것도 보장하지 못한다. 절단된 모수 위에서 세면 통과해 버린다
# — 실제로 314종목만 적재된 상태에서 205를 세고 조용히 통과했다. 아래 커버리지 검사와
# 함께 봐야 의미가 있다 (점검 2026-08-22 결함 2·4).
RANKING_MEANINGFUL_THRESHOLD = 150

# ── 유니버스 커버리지 ───────────────────────────────────
# 모집단(시총 하한을 넘는 거래 가능 보통주) 대비 지표까지 확보된 비율.
# 값이 아니라 값이 나온 모수를 검사한다. 적재가 조용히 잘려도 여기서 드러난다.
UNIVERSE_COVERAGE_WARN = 0.95  # 이 아래면 data_quality 에 경고
UNIVERSE_COVERAGE_REFUSE = 0.70  # 이 아래면 팩 생성 자체를 거부

# ── 브리핑 ──────────────────────────────────────────────
BRIEFING_MAX_AGE_HOURS = 36
# "이번 사이클에 새로 나온 브리핑"의 판정 창.
# premarket(08:20)에서 새 정보인 kr-close-deep 은 전일 18:00 이므로 당일 여부로 판정할 수 없다.
FRESH_WINDOW_HOURS = 24

# 사이클별로 "이번에 새로 반영되는" 브리핑 종류
CYCLE_FRESH_KINDS: dict[str, tuple[str, ...]] = {
    "premarket": ("kr-close-deep", "us-close", "kr-premarket-deep"),
    "midday": ("daily-economy",),
    "preclose": ("kr-preclose",),
    "postmarket": ("kr-close-deep",),
    "event": (),
}
CYCLES = tuple(CYCLE_FRESH_KINDS)

# ── 데이터 신선도 ───────────────────────────────────────
# 거래일 기준이다. 달력일로 세면 금요일 배치 → 월요일 아침이 3일 낡음으로 잡혀
# 매주 월요일의 세 사이클이 통째로 거부된다 (점검 2026-08-23 치명 E).
MAX_OHLCV_STALE_SESSIONS = 2  # 이보다 낡으면 팩 생성 거부
MAX_FLOWS_STALE_SESSIONS = 1  # 이보다 낡으면 경고. **거래일 기준이다**
# 달력일로 세면 매주 월요일 아침마다 경고가 뜬다 — 금요일이 마지막 거래일이라
# 3일 낡은 것으로 계산되기 때문이다. 일봉이 이미 겪고 고친 문제인데(11.9) 수급에만
# 남아 있었다. 같은 함수(_sessions_missed)를 쓴다.
MAX_PARSE_WARNINGS = 30  # 브리핑 경고 누적 임계

# 종목수 × 종목비중 상한이 이보다 작으면 설정 실수로 본다(단위·소수점 착오).
# 100 이 아니라 50 인 이유: 현금을 남기는 설계는 정상이고, 96% 같은 값에 경고를 띄우면
# data_quality 가 상시 오염된다. 진짜 실수는 2%·5% 처럼 자릿수가 다르게 나타난다.
MIN_DEPLOYABLE_PCT = 50.0

# ── 토큰 예산 ───────────────────────────────────────────
MAX_PACK_TOKENS = 25_000
CHARS_PER_TOKEN = 2.5  # 한국어는 토큰 밀도가 높다. 보수적으로 잡는다.


# ── 리스크 한도 (환경변수로만 주입) ─────────────────────
# ADR 0004: 운용 자금 규모와 리스크 한도는 저장소에 커밋하지 않는다.
#
# 예전에는 여기 "보수적 초안 기본값"이 박혀 있었다. 두 가지가 동시에 틀렸다.
#   1. 그 숫자들이 public 저장소에 그대로 올라가 ADR 0004 를 위반했다.
#   2. .env.example 에 AIK_* 가 하나도 없어서, 아무도 설정하지 않은 채
#      초안 값으로 돌고 있었다 — 그리고 그 사실이 겉으로 드러나지 않았다.
#
# 그래서 기본값을 없앤다. 값이 없으면 멈춘다. 조용히 이전 숫자로 돌아가는 경로가
# 아예 존재하지 않아야 한다 (ADR 0006 F5 — 규율은 강제될 때만 엣지다).


class PackRefused(RuntimeError):
    """컨텍스트 팩을 만들 수 없는 상태. 팩을 만들지 않는 것이 정답인 경우다.

    `decision.pack` 이 이 이름을 재수출한다 — 기존 `from decision.pack import PackRefused`
    는 그대로 동작한다. 여기 둔 이유는 config 가 pack 을 import 할 수 없기 때문이다.
    """


class PackImmutable(RuntimeError):
    """이미 저장된 팩을 **다른 내용으로** 덮어쓰려 했다.

    팩은 판단의 근거다. 결정이 `pack_id` 를 참조하는 순간, 팩이 가변이면
    "이 판단이 무엇을 보고 내려졌는가"를 증명할 수 없다 — 감사 사슬의 첫 고리가 끊긴다.
    백테스트 리플레이에서는 더 위험하다 (ADR 0007 선행 조치).

    같은 내용의 재빌드는 이 예외를 내지 않는다. 막는 것은 **내용이 달라지는 덮어쓰기**뿐이다.
    """


class RiskLimitError(PackRefused):
    """리스크 한도 환경변수가 없거나, 숫자가 아니거나, 말이 안 되는 값일 때.

    PackRefused 의 하위다. 한도 없이 만든 팩은 AI 에게 "제한이 없다"고 말하는 것과 같으므로,
    이것도 결국 '팩을 만들 수 없는 상태'의 한 종류다. 기존 거부 처리 경로를 그대로 탄다.
    """


# 이름 → (형, 하한, 상한, 단위 설명). 상한은 물리적·상식적 한계다.
# "소수점 한 칸 잘못 찍어 계좌를 날리는" 종류의 사고를 여기서 끊는다.
_LIMIT_SPECS: dict[str, tuple[type, float, float, str]] = {
    "AIK_MAX_POSITIONS": (int, 1, 20, "동시 보유 종목 수"),
    "AIK_MAX_NEW_ENTRIES_PER_CYCLE": (int, 0, 10, "사이클당 신규 진입 수"),
    "AIK_MAX_WEIGHT_PCT_PER_NAME": (float, 0.1, 50.0, "종목당 비중 상한 (%)"),
    "AIK_MAX_WEIGHT_PCT_PER_SECTOR": (float, 0.1, 100.0, "섹터당 비중 상한 (%)"),
    "AIK_MAX_RISK_PCT_PER_TRADE": (float, 0.01, 5.0, "1회 최대 손실 (계좌 대비 %)"),
    "AIK_DAILY_LOSS_LIMIT_KRW": (int, 0, 10_000_000_000, "일일 손실 한도 (원)"),
    "AIK_MAX_ORDER_VS_ADV_PCT": (float, 0.1, 50.0, "주문금액 / 20일 평균거래대금 (%)"),
    "AIK_PAPER_EQUITY_KRW": (int, 1_000_000, 100_000_000_000, "페이퍼 계좌 시드 (원)"),
    # 갭 가드 (ADR 0009). **고정 % 가 아니라 ATR 배수다** — ATR 4% 종목과 13% 종목에
    # 같은 잣대를 대면 안 된다. 실례: 삼화콘덴서 갭 -2.81% 는 ATR 9.9% 의 0.28배지만
    # 같은 -2.81% 가 ATR 4% 종목에서는 0.70배로 성격이 전혀 다르다.
    # 기본값을 두지 않는 이유는 다른 한도와 같다 — 아무도 설정하지 않은 채 도는 것을 막는다.
    "AIK_MAX_ENTRY_GAP_UP_ATR": (float, 0.05, 5.0, "진입 허용 상방 갭 (ATR 배수)"),
    "AIK_MAX_ENTRY_GAP_DOWN_ATR": (float, 0.05, 5.0, "진입 허용 하방 갭 (ATR 배수)"),
}


# ASCII 숫자만. 지수 표기와 언더바 구분자는 받지 않는다.
_INT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")


def _require(name: str) -> float | int:
    """환경변수를 읽어 검증한다. 실패는 전부 예외다 — 폴백은 없다."""
    spec = _LIMIT_SPECS[name]
    kind, lo, hi, label = spec

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise RiskLimitError(
            f"{name} 이 설정되지 않았다 ({label}). "
            f".env.example 을 .env 로 복사해 값을 채워라. "
            f"허용 범위: {lo} ~ {hi}"
        )
    raw = raw.strip()

    # 파이썬의 int()/float() 는 전각 숫자(８), 아랍-인도 숫자(٨), 자릿수 구분 언더바(1_0)를
    # 조용히 받아들인다. 손으로 고치는 .env 에서 1_0 이 10 이 되는 것은 사고다. 형식을 못박는다.
    pattern = _INT_RE if kind is int else _FLOAT_RE
    if not pattern.fullmatch(raw):
        # 예전에는 여기서 조용히 기본값으로 돌아갔다. 설정한 줄 알았는데 아니었다.
        raise RiskLimitError(
            f"{name}={raw!r} 을 {kind.__name__} 으로 읽을 수 없다 ({label}). "
            + (
                "정수만 쓴다 — 소수점·지수 표기는 받지 않는다."
                if kind is int
                else "십진 소수만 쓴다."
            )
        )
    value: float | int = int(raw) if kind is int else float(raw)

    if not (lo <= value <= hi):
        raise RiskLimitError(
            f"{name}={value} 이 허용 범위를 벗어났다 ({label}, {lo} ~ {hi}). "
            f"단위나 소수점을 확인하라."
        )
    return value


def _check_coherent(c: dict) -> None:
    """항목별 범위를 다 통과해도 조합이 모순일 수 있다. 그 조합을 여기서 끊는다.

    범위 검사는 한 칸씩만 본다 — 두 칸의 어긋남이 실제로는 더 흔하다.
    """
    if c["max_weight_pct_per_name"] > c["max_weight_pct_per_sector"]:
        raise RiskLimitError(
            f"종목 비중 상한({c['max_weight_pct_per_name']}%)이 "
            f"섹터 비중 상한({c['max_weight_pct_per_sector']}%)보다 크다. "
            "한 종목이 자기 섹터에조차 들어가지 못하므로 종목 한도가 무의미해진다."
        )
    if c["max_new_entries_this_cycle"] > c["max_positions"]:
        raise RiskLimitError(
            f"사이클당 신규 진입({c['max_new_entries_this_cycle']})이 "
            f"동시 보유 상한({c['max_positions']})보다 크다. 들어갈 자리보다 많이 사려는 설정이다."
        )


def constraints() -> dict:
    """AI 와 실행 계층이 함께 지키는 한도. 하나라도 없으면 팩을 만들지 않는다."""
    c = {
        "max_positions": _require("AIK_MAX_POSITIONS"),
        "max_new_entries_this_cycle": _require("AIK_MAX_NEW_ENTRIES_PER_CYCLE"),
        "max_weight_pct_per_name": _require("AIK_MAX_WEIGHT_PCT_PER_NAME"),
        "max_weight_pct_per_sector": _require("AIK_MAX_WEIGHT_PCT_PER_SECTOR"),
        # 1회 최대 손실(계좌 대비 %). 포지션 크기 = (총자산 × 이 값) ÷ 손절폭
        "max_risk_pct_per_trade": _require("AIK_MAX_RISK_PCT_PER_TRADE"),
        "daily_loss_limit_krw": _require("AIK_DAILY_LOSS_LIMIT_KRW"),
        "max_order_vs_adv_pct": _require("AIK_MAX_ORDER_VS_ADV_PCT"),
        # 봉투(ADR 0009) — AI 가 고르는 값이 아니라 실행 계층이 강제한다.
        # 그럼에도 팩에 싣는 이유는 ADR 0003 원칙 1 이다: 팩에 없는 것은 AI 에게 없다.
        # 집행되지 않을 진입을 계속 내는 것이 낭비이므로 알려 준다.
        "max_entry_gap_up_atr": _require("AIK_MAX_ENTRY_GAP_UP_ATR"),
        "max_entry_gap_down_atr": _require("AIK_MAX_ENTRY_GAP_DOWN_ATR"),
    }
    _check_coherent(c)
    return c


def account_seed() -> dict:
    """페이퍼 단계의 계좌 상태. 실행 계층이 생기면 실제 잔고로 대체된다.

    가상 자금이어도 '자금 규모'이므로 ADR 0004 대상이다. 게다가 실제 시드와 다른 값으로
    페이퍼를 돌리면 비중·손절폭 계산이 전부 실제와 다르게 나온다.
    """
    return {"total_equity_krw": _require("AIK_PAPER_EQUITY_KRW"), "is_mock": True}


def missing_limits() -> list[str]:
    """고쳐야 할 것들. 진단·기동 점검용.

    하나씩 고쳐가며 재실행하지 않아도 되게 전부 한 번에 모아 준다.
    원소는 보통 환경변수 이름이지만, 항목별로는 멀쩡한데 조합이 모순인 경우에는
    `"(조합 모순) ..."` 로 시작하는 설명 문장이 들어간다 — 이름만 기대하고 쓰지 말 것.
    """
    bad = []
    for name in _LIMIT_SPECS:
        try:
            _require(name)
        except RiskLimitError:
            bad.append(name)
    if not bad:
        # 개별 항목은 다 멀쩡한데 조합이 모순인 경우.
        try:
            constraints()
        except RiskLimitError as e:
            bad.append(f"(조합 모순) {e}")
    return bad


# ── 수수료·세금 (순수익률 계산) ─────────────────────────
# 총수익률과 순수익률을 섞으면 익절 기준이 조용히 어긋난다.
COMMISSION_RATE = 0.00015  # 매수·매도 각각
TAX_RATE = 0.0018  # 매도 시 (거래세)
