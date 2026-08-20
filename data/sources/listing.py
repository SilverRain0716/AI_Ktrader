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


def fetch_listed(markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """현재 상장 종목 마스터.

    두 소스를 합친다 — FDR의 시장별 목록에는 업종·상장일이 없고,
    KRX-DESC에는 시가총액이 없다.

    Returns:
        columns = [code, name, market, sector, industry, listing_date,
                   market_cap, shares, is_preferred, is_spac]
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
            "sector": desc.get("Sector"),
            "industry": desc.get("Industry"),
            "listing_date": pd.to_datetime(desc.get("ListingDate"), errors="coerce"),
        }
    )
    out = out.merge(desc_slim, on="code", how="left")
    out["listing_date"] = out["listing_date"].dt.date

    out = out[out["market"].isin(markets)].copy()

    name = out["name"].fillna("")
    # 보통주는 코드가 0으로 끝난다. 우선주·신주인수권은 그렇지 않다.
    out["is_preferred"] = ~out["code"].str.endswith("0")
    out["is_spac"] = name.str.contains("|".join(_EXCLUDE_NAME_PATTERNS), regex=True, na=False)

    out = out.dropna(subset=["code", "name"]).drop_duplicates("code")
    return out[
        [
            "code",
            "name",
            "market",
            "sector",
            "industry",
            "listing_date",
            "market_cap",
            "shares",
            "is_preferred",
            "is_spac",
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
