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


def test_미래_스키마_버전은_거부한다(tmp_path):
    """구버전 코드로 최신 DB를 열면 조용히 망가진다. 열기 전에 막는다.

    (v2부터 전진 마이그레이션을 지원하므로 '과거 버전'은 더 이상 예외가 아니다.
     전진 동작은 tests/test_dart.py::test_v1_db가_v2로_전진한다 에서 검증한다.)
    """
    c = sqlite3.connect(tmp_path / "v.db", isolation_level=None)
    store.init_db(c)
    c.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="최신"):
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
            "sector_group": ["기타"] * 3,
            "industry": [None] * 3,
            "dept": [None] * 3,
            "is_managed": [False] * 3,
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


def test_KOSDAQ_GLOBAL은_KOSDAQ으로_정규화된다():
    """FDR은 코스닥 우량주 50종목을 'KOSDAQ GLOBAL'로 준다.
    이걸 빼면 알테오젠·에코프로비엠 같은 코스닥 대장주가 통째로 빠진다 (실제로 그랬다)."""
    from data.sources import listing as ls

    assert "KOSDAQ GLOBAL" in ls._MARKETS
    assert ls._MARKET_NORMALIZE["KOSDAQ GLOBAL"] == "KOSDAQ"


# ── 업종 대분류 (점검 2026-08-22 결함 8) ─────────────────
# 업종 문자열은 전부 실제 KRX-DESC 값이다. 지어낸 문자열로 테스트하면
# 분류가 현실과 어긋나도 통과한다.
#
# 이 분류의 목적은 "같이 움직이는 종목에 몰리지 않는 것"(섹터 비중 한도 30%)이다.
# '기타'가 크면 한도가 정확히 작동해야 할 곳에서 작동하지 않는다.


@pytest.mark.parametrize(
    "industry,group",
    [
        # 한국 시장에서 상관관계가 가장 높은 테마. '기타'에 있으면 한도가 무의미하다.
        ("일차전지 및 이차전지 제조업", "2차전지"),  # LG에너지솔루션·삼성SDI·에코프로비엠
        # '의약품'만 보면 삼성바이오로직스·셀트리온이 빠진다.
        ("기초 의약물질 제조업", "제약·바이오"),
        ("완제 의약품 제조업", "제약·바이오"),
        ("기타 식품 제조업", "유통·소비재"),  # CJ제일제당·농심·오리온
        ("담배 제조업", "유통·소비재"),  # KT&G
        ("동·식물성 유지 및 낙농제품 제조업", "유통·소비재"),
        ("절연선 및 케이블 제조업", "반도체·전자"),
        ("컴퓨터 및 주변장치 제조업", "반도체·전자"),
        ("시멘트, 석회, 플라스터 및 그 제품 제조업", "건설·부동산"),
        ("유리 및 유리제품 제조업", "건설·부동산"),
        ("구조용 금속제품, 탱크 및 증기발생기 제조업", "철강·금속"),
        ("유원지 및 기타 오락관련 서비스업", "서비스·레저"),  # 강원랜드·파라다이스·GKL
        ("여행사 및 기타 여행보조 서비스업", "서비스·레저"),
        ("회사 본부 및 경영 컨설팅 서비스업", "지주·상사"),
        ("상품 중개업", "지주·상사"),  # 포스코인터내셔널·LX인터내셔널
        ("증기, 냉·온수 및 공기조절 공급업", "에너지·유틸리티"),
        ("기타 정보 서비스업", "IT·소프트웨어"),
        ("창작 및 예술관련 서비스업", "통신·미디어"),
        # 기존 분류가 규칙 확장으로 흔들리지 않아야 한다
        ("반도체 제조업", "반도체·전자"),
        ("자동차용 엔진 및 자동차 제조업", "자동차"),
        ("기타 화학제품 제조업", "화학"),
        ("특수 목적용 기계 제조업", "기계·장비"),
        ("은행 및 저축기관", "금융"),
        ("선박 및 보트 건조업", "조선·방산·항공"),
    ],
)
def test_업종_대분류(industry, group):
    from data.sources import listing as ls

    assert ls.sector_group(industry) == group


def test_분류_불명은_기타로_남는다():
    from data.sources import listing as ls

    assert ls.sector_group("그외 기타 전문, 과학 및 기술 서비스업") == "기타"
    assert ls.sector_group("") == "기타"
    assert ls.sector_group(None) == "기타"


def test_NaN_업종에도_깨지지_않는다():
    """`if not industry` 는 NaN을 통과시켜 TypeError를 냈고, sector_group 이 전부 NULL이 됐다."""
    import numpy as np

    from data.sources import listing as ls

    assert ls.sector_group(np.nan) == "기타"


def test_분류_우선순위가_좁은_규칙_먼저다():
    """'전지 제조'가 '기계'보다 먼저 와야 이차전지가 기계·장비로 새지 않는다."""
    from data.sources import listing as ls

    groups = [g for g, _ in ls._SECTOR_GROUPS]
    assert groups.index("2차전지") < groups.index("기계·장비")
    assert groups.index("제약·바이오") < groups.index("화학")


# ── 지표 정의 (점검 2026-08-23 F·G·H) ───────────────────


def _bars(n=250, **over):
    import pandas as pd

    d = {
        "date": pd.date_range("2025-01-01", periods=n).date,
        "open": [10000] * n,
        "high": [10000] * n,
        "low": [10000] * n,
        "close": [10000] * n,
        "volume": [1000] * n,
        "halted": [0] * n,
    }
    df = pd.DataFrame(d)
    for k, (idx, val) in over.items():
        df.loc[idx, k] = val
    return df


def test_52주고가는_장중_고가를_쓴다():
    """종가 최댓값을 쓰면 장중에만 찍은 고가를 놓친다. max(close) <= max(high) 이므로
    오차가 항상 한 방향 — 갭이 0에 가깝게 나와 '신고가 근접' 위양성이 된다."""
    from data import indicators as I

    r = I.compute(_bars(high=(100, 20000)), market_cap_krw=1e12, benchmark=None)
    assert r.high_52w_gap_pct == pytest.approx(-50.0), r.high_52w_gap_pct


def test_봉이_부족하면_52주라_부르지_않는다():
    from data import indicators as I

    r = I.compute(_bars(n=150), market_cap_krw=1e12, benchmark=None)
    assert r.high_52w_gap_pct is None


def test_거래량비율은_당일을_분모에_넣지_않는다():
    """당일을 포함하면 급증이 축소된다 — 10배가 6.9배로 읽힌다."""
    from data import indicators as I

    r = I.compute(_bars(volume=(249, 10000)), market_cap_krw=1e12, benchmark=None)
    assert r.volume_ratio == pytest.approx(10.0), r.volume_ratio


def test_단위_필드명이_값과_일치한다():
    """`_bil_krw` 는 billion(10억)인데 값은 억원이었다. 소비자가 LLM 이라 이름대로 읽는다."""
    from data import indicators as I

    r = I.compute(_bars(), market_cap_krw=1_000_000_000_000, benchmark=None)
    assert r.market_cap_eok_krw == pytest.approx(10_000)  # 1조 = 10,000억
    assert not hasattr(r, "market_cap_bil_krw")


# ── 관리종목 판정 (점검 2026-08-23 치명 D) ──────────────


def test_소속부로_관리종목과_투자주의환기를_잡는다():
    from data.sources import listing as ls

    assert ls.is_managed_dept("관리종목(소속부없음)") is True
    assert ls.is_managed_dept("투자주의환기종목(소속부없음)") is True
    assert ls.is_managed_dept("우량기업부") is False
    assert ls.is_managed_dept(None) is False
    assert ls.is_managed_dept("") is False


def test_KOSPI는_소속부가_없어_이_신호만으로는_판정할_수_없다():
    """FDR 소속부는 코스닥 전용이다 — KOSPI 942종목이 전부 결측이라
    is_managed 가 코스닥 전용 필터로 조용히 동작했다. 네이버와 합집합이어야 한다."""
    from data.sources import listing as ls

    assert ls.is_managed_dept(None) is False  # KOSPI 의 실제 값
