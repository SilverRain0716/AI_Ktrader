"""데이터 계층 테스트.

네트워크를 타는 테스트는 `-m net`으로 분리한다. CI에서는 기본 제외한다 —
외부 사이트 장애가 CI 실패로 둔갑하면 신호가 무의미해진다.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from data import indicators, store
from data.sources import naver

# ── 파서 ────────────────────────────────────────────────


def test_parse_sise_json_정상():
    raw = """
    [['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],
    ["20260818", 100, 110, 95, 105, 1000, 52.1],
    ["20260819", 105, 120, 104, 118, 2000, 52.3]]
    """
    rows = naver._parse_sise_json(raw, "000000")
    assert len(rows) == 2
    assert rows[0][0] == "20260818"


def test_parse_sise_json_헤더가_바뀌면_예외():
    """헤더 변경을 조용히 넘기면 엉뚱한 컬럼이 가격으로 저장된다."""
    raw = "[['일자', '시가'], [\"20260818\", 100]]"
    with pytest.raises(naver.NaverFetchError, match="헤더가 바뀌었다"):
        naver._parse_sise_json(raw, "000000")


def test_parse_sise_json_빈응답은_예외():
    with pytest.raises(naver.NaverFetchError):
        naver._parse_sise_json("   ", "000000")


# ── 지표 ────────────────────────────────────────────────


def _synthetic_ohlcv(n: int = 300, start_price: float = 10000.0) -> pd.DataFrame:
    """재현 가능한 상승 추세 데이터. 난수 시드를 고정한다."""
    rng = np.random.default_rng(42)
    steps = rng.normal(loc=0.0015, scale=0.015, size=n)
    close = start_price * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    open_ = np.concatenate([[start_price], close[:-1]])
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="B").date,
            "open": open_.astype("int64"),
            "high": high.astype("int64"),
            "low": low.astype("int64"),
            "close": close.astype("int64"),
            "volume": rng.integers(100_000, 500_000, n),
            "foreign_hold_pct": 50.0,
            "halted": False,
        }
    )


def test_지표_계산_정상():
    ind = indicators.compute(_synthetic_ohlcv())
    d = ind.to_dict()
    assert d["close"] > 0
    assert 0 <= d["rsi14"] <= 100
    assert d["atr14"] > 0
    assert d["atr_pct"] > 0
    assert d["ma_aligned"] in (True, False)
    assert d["high_52w_gap_pct"] <= 0  # 고점 대비이므로 0 이하


def test_봉이_부족하면_추세지표는_None():
    """데이터가 모자랄 때 0이나 임의값을 채우면 AI가 없는 근거로 판단한다."""
    ind = indicators.compute(_synthetic_ohlcv(n=30))
    d = ind.to_dict()
    assert d["close"] is not None
    assert d["rsi14"] is None
    assert d["atr14"] is None
    assert d["ma20"] is None


def test_빈_데이터는_전부_None():
    d = indicators.compute(pd.DataFrame()).to_dict()
    assert all(v is None for v in d.values())


def test_지표_출력에_NaN이_없다():
    """NaN이 JSON에 들어가면 다운스트림이 조용히 깨진다."""
    import json
    import math

    d = indicators.compute(_synthetic_ohlcv(n=150)).to_dict()
    for k, v in d.items():
        if isinstance(v, float):
            assert not math.isnan(v), f"{k}가 NaN"
            assert not math.isinf(v), f"{k}가 Inf"
    json.dumps(d)  # 직렬화 가능해야 한다


def test_상대강도_날짜정렬():
    """휴장일 차이로 인덱스가 어긋나면 rs20이 엉뚱해진다. 날짜로 병합해야 한다."""
    stock = _synthetic_ohlcv(n=200)
    bench = _synthetic_ohlcv(n=200, start_price=2500.0)
    bench = bench.drop(index=[10, 50, 90]).reset_index(drop=True)  # 휴장일 흉내
    ind = indicators.compute(stock, benchmark=bench)
    assert ind.rs20 is not None


def test_연속순매수_일수():
    s = pd.Series([-100, 200, 300, 400])
    assert indicators._consecutive_positive(s) == 3
    assert indicators._consecutive_positive(pd.Series([-1, -2])) == 0
    assert indicators._consecutive_positive(pd.Series([])) == 0


# ── 저장소 ──────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    store.init_db(c)
    yield c
    c.close()


def test_스키마_버전_불일치는_예외(tmp_path):
    c = sqlite3.connect(tmp_path / "v.db", isolation_level=None)
    store.init_db(c)
    c.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="스키마 버전 불일치"):
        store.init_db(c)
    c.close()


def test_ohlcv_업서트는_멱등(conn):
    df = _synthetic_ohlcv(n=10)
    assert store.upsert_ohlcv(conn, "005930", df) == 10
    assert store.upsert_ohlcv(conn, "005930", df) == 10
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 10


def test_거래정지일은_기본적으로_제외된다(conn):
    df = _synthetic_ohlcv(n=10)
    df.loc[5, ["open", "high", "low", "volume"]] = 0
    df.loc[5, "halted"] = True
    store.upsert_ohlcv(conn, "005930", df)

    assert len(store.load_ohlcv(conn, "005930")) == 9
    assert len(store.load_ohlcv(conn, "005930", exclude_halted=False)) == 10


def test_빈_종목마스터로는_교체하지_않는다(conn):
    with pytest.raises(ValueError):
        store.replace_listing(conn, pd.DataFrame(), updated_at="now")


def test_우선주_스팩_제외(conn):
    df = pd.DataFrame(
        {
            "code": ["005930", "005935", "123456"],
            "name": ["삼성전자", "삼성전자우", "아무개제3호스팩"],
            "market": ["KOSPI"] * 3,
            "sector": [None] * 3,
            "industry": [None] * 3,
            "listing_date": [pd.NaT] * 3,
            "market_cap": [1e12, 1e11, 1e9],
            "shares": [1e9] * 3,
            "is_preferred": [False, True, False],
            "is_spac": [False, False, True],
        }
    )
    store.replace_listing(conn, df, updated_at="now")
    assert store.tradable_codes(conn) == ["005930"]


# ── 네트워크 (기본 제외) ─────────────────────────────────


@pytest.mark.net
def test_네이버_액면분할_구간_실측():
    """삼성전자 2018-05-04 50:1 액면분할.

    - 수정주가면 분할 전 종가가 5만원대로 나온다 (원본가면 265만원)
    - 2018-04-30~05-03은 거래정지(halted)로 잡혀야 한다
    """
    df = naver.fetch_ohlcv("005930", date(2018, 4, 20), date(2018, 5, 10))
    assert not df.empty

    before = df[df["date"] == date(2018, 4, 27)].iloc[0]
    assert 40_000 < before["close"] < 70_000, "수정주가가 아니다"

    halted = df[df["halted"]]
    assert len(halted) == 3
    assert (halted["volume"] == 0).all()
