"""정량 지표 계산.

컨텍스트 팩의 `indicators` / `flows` 블록을 채운다.
(schemas/context_pack.schema.json 의 $defs.indicators 와 키가 일치해야 한다)

원칙:
- **확인 불가한 값은 None으로 둔다.** 임의로 채우면 AI가 없는 근거로 판단한다.
- 거래정지일(0값 행)은 입력 단계에서 이미 제거된 상태를 전제한다 (store.load_ohlcv).
- 지표 계산에 필요한 최소 봉 수에 미달하면 전부 None을 반환한다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import talib

from data import config

log = logging.getLogger(__name__)


@dataclass
class Indicators:
    """schemas/context_pack.schema.json $defs.indicators 와 1:1 대응."""

    close: int | None = None
    change_pct: float | None = None
    ma5: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma_aligned: bool | None = None
    disparity20_pct: float | None = None
    rsi14: float | None = None
    macd_hist: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    rs20: float | None = None
    high_52w_gap_pct: float | None = None
    adv20_bil_krw: float | None = None
    volume_ratio: float | None = None
    market_cap_bil_krw: float | None = None

    def to_dict(self) -> dict:
        return {k: _clean(v) for k, v in asdict(self).items()}


def _clean(v):
    """NaN/Inf를 None으로. JSON에 NaN이 들어가면 다운스트림이 조용히 깨진다."""
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    return v


def compute(
    ohlcv: pd.DataFrame,
    *,
    benchmark: pd.DataFrame | None = None,
    market_cap_krw: float | None = None,
) -> Indicators:
    """마지막 봉 기준 지표를 계산한다.

    Args:
        ohlcv: store.load_ohlcv() 결과 (거래정지일 제외됨, date 오름차순)
        benchmark: 상대강도(rs20) 계산용 지수 일봉. 없으면 rs20=None
        market_cap_krw: 시가총액(원). 없으면 market_cap_bil_krw=None
    """
    n = len(ohlcv)
    if n == 0:
        return Indicators()

    close = ohlcv["close"].astype("float64").to_numpy()
    high = ohlcv["high"].astype("float64").to_numpy()
    low = ohlcv["low"].astype("float64").to_numpy()
    volume = ohlcv["volume"].astype("float64").to_numpy()

    ind = Indicators(close=int(close[-1]))

    if n >= 2 and close[-2] > 0:
        ind.change_pct = (close[-1] / close[-2] - 1.0) * 100.0

    if n < config.MIN_BARS_FOR_INDICATORS:
        log.debug(
            "봉 부족 (%d < %d) — 추세·모멘텀 지표는 계산하지 않는다",
            n,
            config.MIN_BARS_FOR_INDICATORS,
        )
        return ind

    ma5 = talib.SMA(close, 5)
    ma20 = talib.SMA(close, 20)
    ma60 = talib.SMA(close, 60)
    ind.ma5, ind.ma20, ind.ma60 = _last(ma5), _last(ma20), _last(ma60)

    if None not in (ind.ma5, ind.ma20, ind.ma60):
        ind.ma_aligned = ind.ma5 > ind.ma20 > ind.ma60

    if ind.ma20:
        ind.disparity20_pct = (close[-1] / ind.ma20 - 1.0) * 100.0

    ind.rsi14 = _last(talib.RSI(close, config.RSI_PERIOD))

    _, _, hist = talib.MACD(close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    ind.macd_hist = _last(hist)

    atr = talib.ATR(high, low, close, config.ATR_PERIOD)
    ind.atr14 = _last(atr)
    if ind.atr14 and close[-1] > 0:
        # 손절폭과 포지션 크기의 근거가 되는 값.
        # 손절 = 진입가 - 2×ATR, 수량 = (총자산 × 최대손실%) ÷ (2×ATR)
        ind.atr_pct = ind.atr14 / close[-1] * 100.0

    # 52주 고점 대비 위치
    win = close[-min(n, 252) :]
    hi = float(np.max(win))
    if hi > 0:
        ind.high_52w_gap_pct = (close[-1] / hi - 1.0) * 100.0

    # 거래대금 (억원). 종가×거래량은 근사치다 — 정확한 거래대금 TR이 붙으면 교체한다.
    amount = close * volume
    if n >= config.ADV_PERIOD:
        ind.adv20_bil_krw = float(np.mean(amount[-config.ADV_PERIOD :])) / 1e8
    vol_ma = talib.SMA(volume, config.ADV_PERIOD)
    last_vol_ma = _last(vol_ma)
    if last_vol_ma and last_vol_ma > 0:
        ind.volume_ratio = float(volume[-1]) / last_vol_ma

    # 상대강도: 종목 20일 수익률 − 지수 20일 수익률.
    # 시장이 올라서 오른 것인지, 종목이 강한 것인지 구분하는 유일한 지표.
    if benchmark is not None and not benchmark.empty:
        ind.rs20 = _relative_strength(ohlcv, benchmark, config.RS_PERIOD)

    if market_cap_krw:
        ind.market_cap_bil_krw = float(market_cap_krw) / 1e8

    return ind


def _relative_strength(ohlcv: pd.DataFrame, benchmark: pd.DataFrame, period: int) -> float | None:
    """날짜를 맞춰 비교한다. 인덱스 위치로 자르면 휴장일 차이 때문에 어긋난다."""
    merged = pd.merge(
        ohlcv[["date", "close"]],
        benchmark[["date", "close"]],
        on="date",
        suffixes=("", "_bm"),
    )
    if len(merged) < period + 1:
        return None
    a0, a1 = merged["close"].iloc[-period - 1], merged["close"].iloc[-1]
    b0, b1 = merged["close_bm"].iloc[-period - 1], merged["close_bm"].iloc[-1]
    if a0 <= 0 or b0 <= 0:
        return None
    return float((a1 / a0 - b1 / b0) * 100.0)


def _last(arr) -> float | None:
    if arr is None or len(arr) == 0:
        return None
    v = arr[-1]
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return float(v)


# ── 수급 파생값 ──────────────────────────────────────────


def compute_flows(flows: pd.DataFrame, close_series: pd.Series | None = None) -> dict:
    """schemas/context_pack.schema.json $defs.stockFlows 와 대응.

    금액은 순매수 수량 × 종가로 근사한다. 정확한 순매수 금액은 키움 REST 연동 후 교체.
    """
    out = {
        "foreign_net_days": None,
        "foreign_net_5d_bil_krw": None,
        "inst_net_days": None,
        "inst_net_5d_bil_krw": None,
        "foreign_hold_pct": None,
        "short_ratio_pct": None,  # 공매도는 키움 REST ka10014 연동 후
        "as_of": None,
    }
    if flows is None or flows.empty:
        return out

    f = flows.sort_values("date")
    out["as_of"] = f["date"].iloc[-1].isoformat()
    out["foreign_hold_pct"] = _clean(f["foreign_hold_pct"].iloc[-1])
    out["foreign_net_days"] = _consecutive_positive(f["foreign_net_qty"])
    out["inst_net_days"] = _consecutive_positive(f["inst_net_qty"])

    tail = f.tail(5)
    if close_series is not None and len(close_series) > 0:
        px = float(close_series.iloc[-1])
        out["foreign_net_5d_bil_krw"] = _clean(float(tail["foreign_net_qty"].sum()) * px / 1e8)
        out["inst_net_5d_bil_krw"] = _clean(float(tail["inst_net_qty"].sum()) * px / 1e8)
    return out


def _consecutive_positive(s: pd.Series) -> int:
    """마지막 날부터 연속 순매수 일수. 순매도로 바뀌면 중단."""
    cnt = 0
    for v in reversed(s.tolist()):
        if v is None or v <= 0:
            break
        cnt += 1
    return cnt
