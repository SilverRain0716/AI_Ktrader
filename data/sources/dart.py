"""DART 전자공시 데이터 소스.

공시는 언론 기사가 아니라 원문에서 확보한다. 기사에 실리지 않는 공시를 놓치지 않기 위해서다.

DART OpenAPI 특성:
- 무료, 인증키당 **1일 20,000건**. 우리 사용량(하루 수 회)은 한도의 0.1% 미만이다.
- `status`가 "000"이 아니면 데이터가 아니라 오류다. "013"(데이터 없음)만 정상적인 빈 결과로 취급한다.
- 비상장 계열사 공시는 `stock_code`가 비어 있다. 버리지 않고 저장하되 종목 매칭에서는 제외한다.

알려진 분류 한계 (실제 응답 대조로 확인, 2026-08 기준):
- `주요사항보고서(자기전환사채만기전취득결정)` 은 CB **발행**이 아니라 **회수**인데 `전환사채`로 분류된다.
  방향이 반대이므로 `default_sentiment`가 단정하지 않고 `report_nm` 원문을 AI에게 그대로 넘긴다.
- `타인에대한채무보증결정` 은 우발채무 신호로 유의미하지만 현재 `기타`로 떨어진다.
  브리핑 프롬프트의 기존 노이즈 필터와 카테고리를 맞추기 위해 일부러 확장하지 않았다.
  필요해지면 `schemas/briefing.schema.json` 의 enum과 함께 바꾼다 — 한쪽만 바꾸면 검증이 깨진다.
- `[기재정정]` 접두어가 붙은 정정 공시는 원공시와 함께 잡힌다. `is_correction()`으로 구분한다.

문서: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date

import httpx
import pandas as pd

from data import config

log = logging.getLogger(__name__)

_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# 조회 대상 시장. Y=유가증권, K=코스닥. N(코넥스)·E(기타)는 스윙 대상이 아니라 제외한다.
CORP_CLASSES = ("Y", "K")

_last_call_at = 0.0


class DartError(RuntimeError):
    """DART가 오류를 반환했을 때. 빈 결과로 삼키지 않는다."""


class DartKeyMissing(DartError):
    """인증키가 없을 때. 배치 전체를 죽이지 않고 이 태스크만 건너뛰게 한다."""


# ── 공시 분류 ────────────────────────────────────────────
# 순서가 중요하다. 위에서부터 먼저 맞는 것을 채택하므로 구체적인 것을 앞에 둔다.
# 카테고리 값은 schemas/briefing.schema.json 의 enum과 정확히 일치해야 한다.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("상장폐지", re.compile(r"상장폐지|정리매매|상장적격성")),
    ("불성실공시", re.compile(r"불성실공시")),
    ("최대주주변경", re.compile(r"최대주주\s*변경")),
    ("합병분할", re.compile(r"합병|분할")),
    ("영업양수도", re.compile(r"영업양수|영업양도|자산양수|자산양도")),
    ("감자", re.compile(r"감자")),
    ("무상증자", re.compile(r"무상증자")),
    ("유상증자", re.compile(r"유상증자")),
    ("신주인수권부사채", re.compile(r"신주인수권부사채|BW")),
    ("전환사채", re.compile(r"전환사채|CB\b")),
    ("자기주식", re.compile(r"자기주식|자사주")),
    ("공급계약", re.compile(r"단일판매|공급계약|수주")),
    ("실적", re.compile(r"영업\(?잠정\)?실적|매출액또는손익구조|실적발표|결산실적")),
)

# 시장 영향이 큰 카테고리. '기타'는 컨텍스트 팩의 disclosures 블록에 올리지 않는다.
MATERIAL_CATEGORIES = frozenset(c for c, _ in _RULES)

# 노이즈. 분류에 걸리더라도 이 패턴이면 '기타'로 내린다.
_NOISE = re.compile(r"투자유의안내|정정신고\s*\(보고\)$|기재정정.*안내공시")

# 방향이 명확한 것만 표시한다. 나머지는 '판단보류'로 두고 AI가 맥락과 함께 판단한다.
# 예: 유상증자는 희석이지만 시설투자 목적이면 해석이 달라진다. 기계적 단정이 오히려 해롭다.
_CLEAR_NEGATIVE = frozenset({"불성실공시", "감자", "상장폐지"})


def classify(report_nm: str) -> str:
    """공시명을 카테고리로 분류한다. 해당 없으면 '기타'."""
    name = report_nm or ""
    if _NOISE.search(name):
        return "기타"
    for category, pattern in _RULES:
        if pattern.search(name):
            return category
    return "기타"


def default_sentiment(category: str) -> str:
    """기계적으로 단정할 수 있는 것만 판정한다.

    대부분은 '판단보류'다. 자기주식 취득/처분, 유상증자 목적처럼 같은 카테고리 안에서
    방향이 갈리는 경우가 많아, 여기서 성급히 호재/악재를 붙이면 AI 판단을 오염시킨다.
    """
    return "악재" if category in _CLEAR_NEGATIVE else "판단보류"


def is_material(category: str) -> bool:
    return category in MATERIAL_CATEGORIES


_CORRECTION = re.compile(r"^\s*\[(기재정정|첨부정정|첨부추가|정정)\]")


def is_correction(report_nm: str) -> bool:
    """정정 공시 여부.

    `[기재정정]주요사항보고서(유상증자결정)` 처럼 앞선 공시를 고친 것이다.
    원공시와 함께 잡히므로 중복으로 보이지만, 조건이 바뀐 경우가 많아 버리지 않는다.
    카테고리를 따로 두지 않는 이유는 `report_nm`에 접두어가 그대로 남아 AI가 구분할 수 있어서다.
    """
    return bool(_CORRECTION.match(report_nm or ""))


# ── API ─────────────────────────────────────────────────


def _api_key() -> str:
    key = os.getenv("DART_API_KEY", "").strip()
    if not key:
        raise DartKeyMissing(
            "DART_API_KEY 가 설정되지 않았다. .env 를 확인하라 (발급: https://opendart.fss.or.kr)"
        )
    return key


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    wait = config.DART_REQUEST_INTERVAL_SEC - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _request(params: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, config.DART_MAX_RETRY + 1):
        _throttle()
        try:
            r = httpx.get(_LIST_URL, params=params, timeout=config.DART_TIMEOUT_SEC)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            log.warning("DART 요청 실패 (%d/%d): %s", attempt, config.DART_MAX_RETRY, e)
            time.sleep(1.0 * attempt)
    raise DartError(f"DART 요청이 {config.DART_MAX_RETRY}회 모두 실패") from last_exc


def _check_status(payload: dict) -> bool:
    """정상이면 True, 데이터 없음이면 False, 그 외에는 예외.

    조용히 빈 결과를 반환하면 '그날 공시가 없었다'와 '조회에 실패했다'를 구분할 수 없다.
    """
    status = payload.get("status")
    message = payload.get("message", "")
    if status == "000":
        return True
    if status == "013":  # 조회된 데이터 없음
        return False
    if status == "020":
        raise DartError(f"DART 일일 호출 한도(20,000건) 초과: {message}")
    if status in ("010", "011", "012"):
        raise DartError(f"DART 인증키 문제 [{status}]: {message}")
    raise DartError(f"DART 오류 [{status}]: {message}")


def fetch_disclosures(on: date, corp_classes: tuple[str, ...] = CORP_CLASSES) -> pd.DataFrame:
    """특정 날짜의 공시 목록.

    Returns:
        columns = [rcept_no, rcept_dt, corp_code, code, corp_name, corp_cls,
                   report_nm, category, material, filer, remark, url]
        `code`는 6자리 종목코드이며 비상장 법인은 None이다.
    """
    key = _api_key()
    day = on.strftime("%Y%m%d")
    records: list[dict] = []

    for cls in corp_classes:
        page_no = 1
        while True:
            payload = _request(
                {
                    "crtfc_key": key,
                    "bgn_de": day,
                    "end_de": day,
                    "corp_cls": cls,
                    "page_count": config.DART_PAGE_COUNT,
                    "page_no": page_no,
                }
            )
            if not _check_status(payload):
                break

            items = payload.get("list") or []
            records.extend(_to_record(it) for it in items)

            total_page = int(payload.get("total_page") or 1)
            if page_no >= total_page:
                break
            page_no += 1
            if page_no > config.DART_MAX_PAGES:
                log.warning(
                    "%s %s: 페이지 상한(%d) 도달 — 이후 공시는 누락된다",
                    day,
                    cls,
                    config.DART_MAX_PAGES,
                )
                break

    if not records:
        return _empty()

    df = pd.DataFrame(records).drop_duplicates(subset="rcept_no", keep="last")
    return df.sort_values(["material", "rcept_no"], ascending=[False, True]).reset_index(drop=True)


def _to_record(item: dict) -> dict:
    report_nm = (item.get("report_nm") or "").strip()
    category = classify(report_nm)
    raw_code = (item.get("stock_code") or "").strip()
    return {
        "rcept_no": (item.get("rcept_no") or "").strip(),
        "rcept_dt": (item.get("rcept_dt") or "").strip(),
        "corp_code": (item.get("corp_code") or "").strip(),
        "code": raw_code if re.fullmatch(r"\d{6}", raw_code) else None,
        "corp_name": (item.get("corp_name") or "").strip(),
        "corp_cls": (item.get("corp_cls") or "").strip(),
        "report_nm": report_nm,
        "category": category,
        "material": is_material(category),
        "filer": (item.get("flr_nm") or "").strip(),
        "remark": (item.get("rm") or "").strip(),
        "url": _VIEWER_URL.format(rcept_no=(item.get("rcept_no") or "").strip()),
    }


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "rcept_no",
            "rcept_dt",
            "corp_code",
            "code",
            "corp_name",
            "corp_cls",
            "report_nm",
            "category",
            "material",
            "filer",
            "remark",
            "url",
        ]
    )
