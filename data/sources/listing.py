"""종목 마스터와 상장폐지 이력.

FinanceDataReader를 쓴다. 단, **일봉에는 쓰지 않는다** — FDR의 국내 일봉은
3,000행 상한이 있고 그보다 과거를 요청하면 예외 없이 빈 DataFrame을 돌려준다
(2014-05-29 이전이 조용히 사라진다). 목록 조회 용도로만 쓴다.

상장폐지 목록은 백테스트 생존편향을 막는 유일한 무료 경로다.
"""

from __future__ import annotations

import logging

import FinanceDataReader as fdr
import pandas as pd

log = logging.getLogger(__name__)

# 종목 마스터에서 제외할 증권 종류. 스팩은 이름 규칙으로 따로 거른다.
_EXCLUDE_NAME_PATTERNS = (
    "스팩",
    "제[0-9]+호",
)


class ListingFetchError(RuntimeError):
    pass


# ── 업종 대분류 ─────────────────────────────────────────
# FDR이 주는 Industry 는 한국표준산업분류라 158개로 잘게 쪼개져 있다.
# 1종목뿐인 업종이 22개, 5종목 이하가 73개다. 이대로는 섹터 집중도 한도
# (max_weight_pct_per_sector)가 무의미해진다 — 반도체 두 종목이 '반도체 제조업'과
# '전자부품 제조업'으로 갈리면 한도에 걸리지 않는다.
# 그래서 키워드로 대분류를 파생한다. 위에서부터 먼저 맞는 것을 채택하므로 순서가 중요하다.
_SECTOR_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "반도체·전자",
        ("반도체", "전자부품", "전자제품", "표시장치", "디스플레이", "광학", "인쇄회로"),
    ),
    ("IT·소프트웨어", ("소프트웨어", "컴퓨터 프로그", "정보서비스", "포털", "자료처리", "게임")),
    ("제약·바이오", ("의약품", "생물학적", "연구개발업", "의료용 기기", "의료용품")),
    ("화학", ("화학", "플라스틱", "고무", "비료", "도료")),
    ("기계·장비", ("기계", "장비 제조", "금형", "공구")),
    ("자동차", ("자동차",)),
    ("조선·방산·항공", ("선박", "항공기", "무기", "우주")),
    ("철강·금속", ("1차 금속", "금속 가공", "금속가공", "철강", "비철")),
    ("건설·부동산", ("건설", "토목", "부동산", "건축")),
    ("금융", ("금융", "은행", "보험", "증권", "신탁", "여신")),
    (
        "유통·소비재",
        ("도매", "소매", "음식료", "식료품", "음료", "섬유", "의복", "화장품", "가구", "종이"),
    ),
    ("운송·물류", ("운송", "운수", "해운", "항공 여객", "창고", "물류")),
    ("통신·미디어", ("통신", "방송", "영상", "출판", "광고", "오디오")),
    ("에너지·유틸리티", ("전기", "가스", "석유", "발전", "정련", "수도")),
)


def sector_group(industry: object) -> str:
    """한국표준산업분류 문자열 → 14개 대분류. 매칭 실패는 '기타'.

    pandas 에서 결측은 float('nan') 으로 들어온다. NaN 은 truthy 라
    `if not industry` 로는 걸러지지 않는다 — 실제로 이 때문에 전 종목이 NULL 이 됐다.
    """
    if not isinstance(industry, str) or not industry.strip():
        return "기타"
    for group, keywords in _SECTOR_GROUPS:
        if any(k in industry for k in keywords):
            return group
    return "기타"


# FDR 은 코스닥 우량주 50종목을 'KOSDAQ GLOBAL' 이라는 별도 시장으로 준다.
# 이걸 빼면 알테오젠(17조)·에코프로비엠(10조)·주성엔지니어링(8조) 같은
# **코스닥 대장주가 통째로 빠진다.** 실제로 그렇게 돌아가고 있었고, 조용히 정상으로 보였다.
# KONEX 는 유동성이 낮아 스윙 대상이 아니므로 계속 제외한다.
_MARKETS = ("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL")
# 스키마(context_pack)의 market enum 은 KOSPI|KOSDAQ 뿐이므로 저장 전에 정규화한다.
_MARKET_NORMALIZE = {"KOSDAQ GLOBAL": "KOSDAQ"}


def fetch_listed(markets: tuple[str, ...] = _MARKETS) -> pd.DataFrame:
    """현재 상장 종목 마스터.

    두 소스를 합친다 — FDR의 시장별 목록에는 업종·상장일이 없고,
    KRX-DESC에는 시가총액이 없다.

    ⚠️ FDR의 `Sector` 컬럼은 업종이 아니라 **코스닥 소속부**다
    (벤처기업부·중견기업부·우량기업부·기술성장기업부, 그리고 관리종목·SPAC·외국기업 표시).
    진짜 업종분류는 `Industry` 쪽이다. 이 둘을 바꿔 쓰면 섹터 집중도 판정이 통째로 무의미해진다.

    소속부는 버리지 않고 `dept` 로 보존한다 — 관리종목·투자주의환기종목을
    걸러내는 유일한 무료 신호이기 때문이다.

    Returns:
        columns = [code, name, market, sector, sector_group, industry, dept, listing_date,
                   market_cap, shares, is_preferred, is_spac, is_managed]
    """
    base = _fetch("KRX")
    desc = _fetch("KRX-DESC")

    out = pd.DataFrame()
    out["code"] = base["Code"].astype(str).str.zfill(6)
    out["name"] = base["Name"]
    out["market"] = base["Market"]
    out["market_cap"] = pd.to_numeric(base.get("Marcap"), errors="coerce")
    out["shares"] = pd.to_numeric(base.get("Stocks"), errors="coerce")

    desc_slim = pd.DataFrame(
        {
            "code": desc["Code"].astype(str).str.zfill(6),
            "dept": desc.get("Sector"),  # 실제로는 소속부다
            "industry": desc.get("Industry"),  # 실제 업종분류
            "listing_date": pd.to_datetime(desc.get("ListingDate"), errors="coerce"),
        }
    )
    out = out.merge(desc_slim, on="code", how="left")
    out["listing_date"] = out["listing_date"].dt.date

    out = out[out["market"].isin(markets)].copy()
    out["market"] = out["market"].replace(_MARKET_NORMALIZE)

    name = out["name"].fillna("")
    # 보통주는 코드가 0으로 끝난다. 우선주·신주인수권은 그렇지 않다.
    out["is_preferred"] = ~out["code"].str.endswith("0")
    out["is_spac"] = name.str.contains("|".join(_EXCLUDE_NAME_PATTERNS), regex=True, na=False)

    dept = out["dept"].fillna("").astype(str)
    # 소속부 표시로 관리종목·투자주의환기종목을 잡는다. 거래는 되지만 스윙 대상이 아니다.
    out["is_managed"] = dept.str.contains("관리종목|투자주의환기", regex=True, na=False)
    out["is_spac"] = out["is_spac"] | dept.str.contains("SPAC", na=False)
    out["sector"] = out["industry"]
    out["sector_group"] = out["industry"].map(sector_group)

    out = out.dropna(subset=["code", "name"]).drop_duplicates("code")
    return out[
        [
            "code",
            "name",
            "market",
            "sector",
            "sector_group",
            "industry",
            "dept",
            "listing_date",
            "market_cap",
            "shares",
            "is_preferred",
            "is_spac",
            "is_managed",
        ]
    ].reset_index(drop=True)


def _fetch(key: str) -> pd.DataFrame:
    try:
        df = fdr.StockListing(key)
    except Exception as e:
        raise ListingFetchError(f"{key} 조회 실패: {e}") from e
    if df is None or df.empty:
        raise ListingFetchError(f"{key} 결과가 비어 있다")
    return df


def fetch_delisted() -> pd.DataFrame:
    """상장폐지 이력. 생존편향 방지의 핵심 데이터.

    Returns:
        columns = [code, name, market, listing_date, delisting_date, reason,
                   to_code, to_name]
        to_code/to_name 은 합병 시 승계 종목 — 연속 시계열 재구성에 쓴다.
    """
    try:
        raw = fdr.StockListing("KRX-DELISTING")
    except Exception as e:
        raise ListingFetchError(f"상장폐지 목록 조회 실패: {e}") from e

    if raw is None or raw.empty:
        raise ListingFetchError("상장폐지 목록이 비어 있다")

    # 주권만 남긴다. 신주인수권증서·수익증권 등이 절반 이상을 차지한다.
    if "SecuGroup" in raw.columns:
        raw = raw[raw["SecuGroup"] == "주권"]

    out = pd.DataFrame()
    out["code"] = raw["Symbol"].astype(str).str.zfill(6)
    out["name"] = raw.get("Name")
    out["market"] = raw.get("Market")
    out["listing_date"] = pd.to_datetime(raw.get("ListingDate"), errors="coerce").dt.date
    out["delisting_date"] = pd.to_datetime(raw.get("DelistingDate"), errors="coerce").dt.date
    out["reason"] = raw.get("Reason")
    out["to_code"] = raw.get("ToSymbol")
    out["to_name"] = raw.get("ToName")

    return (
        out.dropna(subset=["code"])
        .drop_duplicates(subset=["code", "delisting_date"])
        .reset_index(drop=True)
    )
