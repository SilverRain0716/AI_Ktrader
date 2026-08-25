"""네이버 금융 데이터 소스.

일봉과 종목별 수급을 가져온다. 인증이 필요 없고 1990년부터 수정주가로 제공된다.

⚠️ 비공식 API다. 약관상 근거가 없고 언제든 막힐 수 있다.
   - 호출 간격을 반드시 둔다 (config.NAVER_REQUEST_INTERVAL_SEC)
   - 실패를 정상 상황으로 취급하고, 조용히 빈 결과를 반환하지 않는다
   - 차단되면 키움 REST ka10081로 갈아탈 수 있도록 반환 형식을 소스 중립적으로 유지한다
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date

import httpx
import pandas as pd

from data import config

log = logging.getLogger(__name__)

_SISE_URL = "https://api.finance.naver.com/siseJson.naver"
_FRGN_URL = "https://finance.naver.com/item/frgn.naver"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_last_call_at = 0.0


class NaverFetchError(RuntimeError):
    """네이버 응답을 신뢰할 수 없을 때. 빈 DataFrame으로 삼키지 않는다."""


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    wait = config.NAVER_REQUEST_INTERVAL_SEC - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _get(url: str, params: dict, *, encoding: str | None = None) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, config.NAVER_MAX_RETRY + 1):
        _throttle()
        try:
            r = httpx.get(
                url,
                params=params,
                headers={"User-Agent": _UA, "Referer": "https://finance.naver.com/"},
                timeout=config.NAVER_TIMEOUT_SEC,
                follow_redirects=True,
            )
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            return r.text
        except Exception as e:
            last_exc = e
            log.warning("네이버 요청 실패 (%d/%d): %s", attempt, config.NAVER_MAX_RETRY, e)
            time.sleep(1.0 * attempt)
    raise NaverFetchError(f"{url} 요청이 {config.NAVER_MAX_RETRY}회 모두 실패") from last_exc


# ── 일봉 ────────────────────────────────────────────────


def fetch_ohlcv(
    symbol: str,
    start: date,
    end: date,
    timeframe: str = "day",
) -> pd.DataFrame:
    """일/주/월봉 OHLCV.

    Args:
        symbol: 6자리 종목코드, 또는 'KOSPI' / 'KOSDAQ' 같은 지수 심볼
        timeframe: day | week | month

    Returns:
        columns = [date, open, high, low, close, volume, foreign_hold_pct, halted]
        halted=True 는 거래정지일(시가=고가=저가=0, 거래량=0)이다.
        이 행을 지표 계산에 넣으면 ATR·RSI·볼린저가 오염된다.
    """
    raw = _get(
        _SISE_URL,
        {
            "symbol": symbol,
            "requestType": 1,
            "startTime": start.strftime("%Y%m%d"),
            "endTime": end.strftime("%Y%m%d"),
            "timeframe": timeframe,
        },
    )
    rows = _parse_sise_json(raw, symbol)
    if not rows:
        return _empty_ohlcv()

    df = pd.DataFrame(
        rows,
        columns=["date", "open", "high", "low", "close", "volume", "foreign_hold_pct"],
    )
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.date
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    df["foreign_hold_pct"] = pd.to_numeric(df["foreign_hold_pct"], errors="coerce")

    # 거래정지일: 시가·고가·저가가 0이고 거래량이 0. 종가만 전일 종가로 채워져 온다.
    df["halted"] = (df["open"] == 0) & (df["volume"] == 0)

    df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
    return df.reset_index(drop=True)


def _parse_sise_json(raw: str, symbol: str) -> list[list]:
    """siseJson 응답은 유효한 JSON이 아니다 — 키는 작은따옴표, 사이에 탭·개행이 섞여 있다."""
    text = raw.strip()
    if not text:
        raise NaverFetchError(f"{symbol}: 빈 응답")

    # 헤더 행의 작은따옴표만 큰따옴표로 바꾼다. 데이터 행은 이미 숫자/큰따옴표다.
    text = re.sub(r"'([^']*)'", r'"\1"', text)
    # 배열 사이의 공백·탭·개행 정리
    text = re.sub(r",\s*\]", "]", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise NaverFetchError(f"{symbol}: siseJson 파싱 실패 — {e}") from e

    if not isinstance(parsed, list) or len(parsed) < 1:
        raise NaverFetchError(f"{symbol}: 예상치 못한 응답 구조")

    header = parsed[0]
    if not (isinstance(header, list) and header and header[0] == "날짜"):
        raise NaverFetchError(f"{symbol}: 헤더가 바뀌었다 — {header!r}. 파서 점검 필요")

    return [r for r in parsed[1:] if isinstance(r, list) and len(r) >= 6]


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["date", "open", "high", "low", "close", "volume", "foreign_hold_pct", "halted"]
    )


# ── 종목별 수급 (기관·외국인) ────────────────────────────


@dataclass(frozen=True)
class FlowRow:
    date: date
    close: int
    volume: int
    inst_net_qty: int
    foreign_net_qty: int
    foreign_hold_qty: int
    foreign_hold_pct: float


_BASIC_URL = "https://m.stock.naver.com/api/stock/{code}/basic"


def fetch_is_managed(code: str) -> bool:
    """관리종목 여부.

    FDR 의 소속부(`Dept`)는 **코스닥에만 있다** — KOSPI 942종목은 전부 결측이라
    `is_managed` 가 코스닥 전용 필터로 조용히 동작하고 있었다 (점검 2026-08-23 치명 D).
    실측에서 KOSPI 소형주 40종목 중 18건이 관리종목이었고 전부 하드 필터를 통과했다.

    네이버 모바일 API 의 `isManagement` 는 두 시장 모두 준다. 정상 종목에는 키 자체가 없다.
    HTML 이 아니라 JSON 이라 구조 변경에 비교적 강하지만, 그래도 외부 사설 API 다 —
    실패는 삼키지 않고 올려서 호출 측이 '판정 불가'로 남기게 한다.
    """
    txt = _get(_BASIC_URL.format(code=code), {})
    try:
        payload = json.loads(txt)
    except json.JSONDecodeError as e:
        raise NaverFetchError(f"{code}: basic 응답이 JSON이 아니다 — {e}") from e
    if not isinstance(payload, dict) or "stockName" not in payload:
        raise NaverFetchError(f"{code}: basic 응답 형식이 바뀌었다 (stockName 없음)")
    return bool(payload.get("isManagement"))


def fetch_investor_flows(code: str, pages: int = 1) -> pd.DataFrame:
    """종목별 기관·외국인 순매매.

    ⚠️ 개인 수급은 이 페이지에 없다. 시장 전체 수급은 fetch_market_flows()를 쓴다.
    페이지당 20영업일치. HTML 테이블 파싱이라 페이지 구조가 바뀌면 깨진다.
    """
    frames: list[pd.DataFrame] = []
    for page in range(1, pages + 1):
        html = _get(_FRGN_URL, {"code": code, "page": page}, encoding="euc-kr")
        try:
            tables = pd.read_html(io.StringIO(html))
        except ValueError as e:
            raise NaverFetchError(f"{code}: 수급 테이블을 찾지 못함 — {e}") from e

        target = _pick_flow_table(tables)
        if target is None:
            raise NaverFetchError(f"{code}: 수급 테이블 구조가 바뀌었다 (page={page})")
        frames.append(target)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df.columns = [
        "date",
        "close",
        "diff",
        "change_pct",
        "volume",
        "inst_net_qty",
        "foreign_net_qty",
        "foreign_hold_qty",
        "foreign_hold_pct",
    ]
    df = df.dropna(subset=["date"])
    df["date"] = pd.to_datetime(df["date"], format="%Y.%m.%d", errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    for col in ("close", "volume", "inst_net_qty", "foreign_net_qty", "foreign_hold_qty"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    # 보유율은 "52.06%" 형태의 문자열로 온다. %를 떼지 않으면 통째로 NaN이 된다.
    df["foreign_hold_pct"] = pd.to_numeric(
        df["foreign_hold_pct"].astype(str).str.replace("%", "", regex=False).str.strip(),
        errors="coerce",
    )
    return df.drop(columns=["diff", "change_pct"]).drop_duplicates("date").sort_values("date")


def _pick_flow_table(tables: list[pd.DataFrame]) -> pd.DataFrame | None:
    """페이지 구조 변경에 대비해 인덱스가 아니라 모양으로 고른다."""
    for t in tables:
        if t.shape[1] == 9 and len(t) > 5:
            return t
    return None
