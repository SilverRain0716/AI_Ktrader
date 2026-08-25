"""판단 계층 설정.

숫자는 전부 여기에 모은다. 흩어지면 "왜 이 값인가"를 추적할 수 없다.
운용 자금·리스크 한도는 저장소에 커밋하지 않고 환경변수로만 받는다 (ADR 0004).
"""

from __future__ import annotations

import os

# ── 유니버스 ────────────────────────────────────────────
# 하드 필터: 통과 못하면 무조건 제외. 슬리피지가 전략을 삼키는 구간을 잘라낸다.
MIN_ADV20_EOK_KRW = 100.0  # 20일 평균 거래대금 하한 (억원)
MIN_MARKET_CAP_EOK_KRW = 3000.0  # 시가총액 하한 (억원)
MIN_BARS = 120  # 지표 계산에 필요한 최소 유효봉
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
MAX_FLOWS_STALE_DAYS = 1  # 이보다 낡으면 경고
MAX_PARSE_WARNINGS = 30  # 브리핑 경고 누적 임계

# ── 토큰 예산 ───────────────────────────────────────────
MAX_PACK_TOKENS = 25_000
CHARS_PER_TOKEN = 2.5  # 한국어는 토큰 밀도가 높다. 보수적으로 잡는다.


# ── 리스크 한도 (환경변수로만 주입) ─────────────────────
# 아래 기본값은 검증 전 단계의 보수적 초안이다. 실계좌 운용 전에 반드시 재검토한다.
def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def constraints() -> dict:
    return {
        "max_positions": _i("AIK_MAX_POSITIONS", 8),
        "max_new_entries_this_cycle": _i("AIK_MAX_NEW_ENTRIES_PER_CYCLE", 2),
        "max_weight_pct_per_name": _f("AIK_MAX_WEIGHT_PCT_PER_NAME", 12.0),
        "max_weight_pct_per_sector": _f("AIK_MAX_WEIGHT_PCT_PER_SECTOR", 30.0),
        # 1회 최대 손실(계좌 대비 %). 포지션 크기 = (총자산 × 이 값) ÷ 손절폭
        "max_risk_pct_per_trade": _f("AIK_MAX_RISK_PCT_PER_TRADE", 0.5),
        "daily_loss_limit_krw": _i("AIK_DAILY_LOSS_LIMIT_KRW", 0),
        "max_order_vs_adv_pct": _f("AIK_MAX_ORDER_VS_ADV_PCT", 3.0),
    }


def account_seed() -> dict:
    """페이퍼 단계의 계좌 상태. 실행 계층이 생기면 실제 잔고로 대체된다."""
    total = _i("AIK_PAPER_EQUITY_KRW", 100_000_000)
    return {"total_equity_krw": total, "is_mock": True}


# ── 수수료·세금 (순수익률 계산) ─────────────────────────
# 총수익률과 순수익률을 섞으면 익절 기준이 조용히 어긋난다.
COMMISSION_RATE = 0.00015  # 매수·매도 각각
TAX_RATE = 0.0018  # 매도 시 (거래세)
