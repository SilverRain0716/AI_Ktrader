"""DART 공시 수집 테스트.

네트워크를 타지 않는다. 분류 규칙과 오류 처리, 스키마 마이그레이션만 검증한다.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

from data import store
from data.sources import dart

# ── 분류 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "report_nm,expected",
    [
        ("영업(잠정)실적(공정공시)", "실적"),
        ("매출액또는손익구조30%(대규모법인은15%)이상변동", "실적"),
        ("단일판매ㆍ공급계약체결", "공급계약"),
        ("주요사항보고서(유상증자결정)", "유상증자"),
        ("주요사항보고서(무상증자결정)", "무상증자"),
        ("주요사항보고서(전환사채권발행결정)", "전환사채"),
        ("주요사항보고서(신주인수권부사채권발행결정)", "신주인수권부사채"),
        ("주요사항보고서(자기주식취득결정)", "자기주식"),
        ("주요사항보고서(회사합병결정)", "합병분할"),
        ("주요사항보고서(감자결정)", "감자"),
        ("최대주주변경", "최대주주변경"),
        ("불성실공시법인지정", "불성실공시"),
        ("상장폐지결정", "상장폐지"),
        ("영업양수도결정", "영업양수도"),
        ("임원ㆍ주요주주특정증권등소유상황보고서", "기타"),
        ("", "기타"),
    ],
)
def test_classify(report_nm, expected):
    assert dart.classify(report_nm) == expected


def test_classify_노이즈는_기타로_내린다():
    """'투자유의안내'는 카테고리 키워드를 품고 있어도 시장 영향이 작다."""
    assert dart.classify("투자유의안내(불성실공시법인지정예고)") == "기타"


def test_구체적인_규칙이_우선한다():
    """신주인수권부사채가 전환사채보다 먼저 매칭되어야 한다."""
    assert dart.classify("주요사항보고서(신주인수권부사채권발행결정)") == "신주인수권부사채"


def test_default_sentiment는_명확한_것만_판정한다():
    assert dart.default_sentiment("불성실공시") == "악재"
    assert dart.default_sentiment("감자") == "악재"
    assert dart.default_sentiment("상장폐지") == "악재"
    # 방향이 갈리는 것은 단정하지 않는다 — AI가 맥락과 함께 판단한다
    assert dart.default_sentiment("유상증자") == "판단보류"
    assert dart.default_sentiment("자기주식") == "판단보류"


def test_카테고리가_브리핑_스키마_enum과_일치한다():
    """스키마와 어긋나면 컨텍스트 팩 검증이 런타임에 깨진다."""
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "briefing.schema.json").read_text(
            encoding="utf-8"
        )
    )
    enum = set(schema["properties"]["disclosures"]["items"]["properties"]["category"]["enum"])
    assert dart.MATERIAL_CATEGORIES.issubset(enum)
    assert "기타" in enum


# ── 응답 처리 ────────────────────────────────────────────


def test_status_000은_정상():
    assert dart._check_status({"status": "000", "message": "정상"}) is True


def test_status_013은_빈결과이지_오류가_아니다():
    assert dart._check_status({"status": "013", "message": "조회된 데이타가 없습니다."}) is False


def test_status_020은_한도초과_예외():
    with pytest.raises(dart.DartError, match="한도"):
        dart._check_status({"status": "020", "message": "요청 제한을 초과"})


@pytest.mark.parametrize("code", ["010", "011", "012"])
def test_인증키_문제는_예외(code):
    with pytest.raises(dart.DartError, match="인증키"):
        dart._check_status({"status": code, "message": "키 오류"})


def test_모르는_status는_조용히_넘기지_않는다():
    with pytest.raises(dart.DartError):
        dart._check_status({"status": "800", "message": "시스템 점검"})


def test_to_record_비상장은_종목코드가_None():
    rec = dart._to_record(
        {
            "corp_code": "00126380",
            "corp_name": "비상장계열사",
            "stock_code": " ",
            "corp_cls": "E",
            "report_nm": "주요사항보고서(유상증자결정)",
            "rcept_no": "20260820000001",
            "rcept_dt": "20260820",
            "flr_nm": "비상장계열사",
        }
    )
    assert rec["code"] is None
    assert rec["category"] == "유상증자"
    assert rec["material"] is True
    assert rec["url"].endswith("20260820000001")


def test_to_record_정상_종목코드():
    rec = dart._to_record(
        {
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "corp_cls": "Y",
            "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서",
            "rcept_no": "20260820000002",
            "rcept_dt": "20260820",
            "flr_nm": "홍길동",
        }
    )
    assert rec["code"] == "005930"
    assert rec["material"] is False


def test_api_key_없으면_전용_예외():
    import os

    saved = os.environ.pop("DART_API_KEY", None)
    try:
        with pytest.raises(dart.DartKeyMissing):
            dart._api_key()
    finally:
        if saved is not None:
            os.environ["DART_API_KEY"] = saved


# ── 저장소 ──────────────────────────────────────────────


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            dart._to_record(
                {
                    "corp_code": "00126380",
                    "corp_name": "삼성전자",
                    "stock_code": "005930",
                    "corp_cls": "Y",
                    "report_nm": "단일판매ㆍ공급계약체결",
                    "rcept_no": "20260820000010",
                    "rcept_dt": "20260820",
                    "flr_nm": "삼성전자",
                }
            ),
            dart._to_record(
                {
                    "corp_code": "00164779",
                    "corp_name": "테스트",
                    "stock_code": "123456",
                    "corp_cls": "K",
                    "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서",
                    "rcept_no": "20260820000011",
                    "rcept_dt": "20260820",
                    "flr_nm": "홍길동",
                }
            ),
        ]
    )


def test_upsert_disclosures_멱등():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    df = _sample_df()
    store.upsert_disclosures(conn, df)
    store.upsert_disclosures(conn, df)  # 같은 날 두 번 돌려도
    assert conn.execute("SELECT COUNT(*) FROM disclosures").fetchone()[0] == 2


def test_load_disclosures_주요공시만():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    store.upsert_disclosures(conn, _sample_df())

    out = store.load_disclosures(conn, start=date(2026, 8, 20), end=date(2026, 8, 20))
    assert len(out) == 1
    assert out.iloc[0]["category"] == "공급계약"

    allrows = store.load_disclosures(
        conn, start=date(2026, 8, 20), end=date(2026, 8, 20), material_only=False
    )
    assert len(allrows) == 2


def test_load_disclosures_종목필터():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    store.upsert_disclosures(conn, _sample_df())
    out = store.load_disclosures(
        conn, start=date(2026, 8, 20), end=date(2026, 8, 20), codes=["999999"]
    )
    assert out.empty


# ── 마이그레이션 ─────────────────────────────────────────


def test_v1_db가_v2로_전진한다():
    """기존 사용자 DB에 disclosures 테이블이 없어도 깨지지 않아야 한다."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE ohlcv (code TEXT, date TEXT, PRIMARY KEY (code, date));PRAGMA user_version=1;"
    )
    store.init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    conn.execute("SELECT COUNT(*) FROM disclosures")  # 존재하면 예외 없음


def test_미래버전_DB는_거부한다():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="최신"):
        store.init_db(conn)


# ── 정정 공시 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "report_nm,expected",
    [
        ("[기재정정]주요사항보고서(유상증자결정)", True),
        ("[첨부정정]주요사항보고서(전환사채권발행결정)", True),
        ("주요사항보고서(유상증자결정)", False),
        ("", False),
    ],
)
def test_is_correction(report_nm, expected):
    assert dart.is_correction(report_nm) is expected


def test_정정공시도_주요공시로_남긴다():
    """조건이 바뀐 정정이 많아 버리면 안 된다."""
    assert dart.classify("[기재정정]주요사항보고서(유상증자결정)") == "유상증자"


def test_CB_회수는_발행과_방향이_반대다():
    """분류는 전환사채로 되지만 방향을 단정하지 않는 것이 중요하다."""
    nm = "주요사항보고서(자기전환사채만기전취득결정)"
    assert dart.classify(nm) == "전환사채"
    assert dart.default_sentiment("전환사채") == "판단보류"


# ── 유니버스 배제 판정 (점검 2026-08-22 결함 4) ──────────
# 공시명은 전부 2026-08 실제 DART 응답에서 가져왔다. 지어낸 문자열로 테스트하면
# 분류가 현실과 어긋나도 통과한다 — 실제로 그렇게 `불성실공시법인미지정`을 놓쳤다.

_확정_배제 = (
    "불성실공시법인지정              (공시불이행)",
    "불성실공시법인지정              (공시번복 1건, 공시변경 1건)",
    "주권매매거래정지              (상장폐지 사유발생)",
    "주권매매거래정지              (상장적격성 실질심사 대상(사유발생))",
    "주권매매거래정지기간변경              (상장폐지 사유 발생)",
    "주권매매거래정지해제              (상장폐지에 따른 정리매매 개시)",
    "기타시장안내              (코스닥시장위원회 개최 결과 및 상장폐지 결정 안내)",
    "기타시장안내              (상장적격성 실질심사 사유 발생)",
    "기타시장안내              (상장폐지 이의신청서 제출)",
    "기타시장안내              (정리매매 보류 관련)",
    "기타경영사항(자율공시)              (상장폐지 결정 효력정지 가처분신청)",
)

# "이미 풀렸다" 또는 "아직 아니다". 카테고리만 보면 전부 악재로 뒤집힌다.
_배제_아님 = (
    "불성실공시법인미지정              (지정유예)",
    "불성실공시법인지정예고              (공시번복)",
    "기타시장안내              (상장적격성 실질심사 대상 제외 결정)",
    "주권매매거래정지해제              (상장적격성 실질심사 대상 제외 결정)",
    "기타시장안내              (상장적격성 실질심사 대상결정 기한 안내)",
    "기타시장안내              (정기보고서 미제출 관련 상장폐지 절차 미진행)",
    "기타시장안내              (상장적격성 실질심사 사유 추가 관련 절차 미진행)",
    "기타시장안내              (시가총액 미달에 따른 상장폐지 우려 관련 안내)",
)

# 해소를 알리는 확정 통지. "아직 아니다"와 구분한다 — 저쪽은 배제도 해제도 하지 않는다.
_해소 = (
    "불성실공시법인미지정              (지정유예)",
    "기타시장안내              (상장적격성 실질심사 대상 제외 결정)",
    "주권매매거래정지해제              (상장적격성 실질심사 대상 제외 결정)",
)


@pytest.mark.parametrize("report_nm", _확정_배제)
def test_확정된_악재는_배제한다(report_nm):
    assert dart.is_disqualifying(report_nm, dart.classify(report_nm)) is True


@pytest.mark.parametrize("report_nm", _배제_아님)
def test_해소되거나_확정되지_않은_공시는_배제하지_않는다(report_nm):
    assert dart.is_disqualifying(report_nm, dart.classify(report_nm)) is False


@pytest.mark.parametrize("report_nm", _해소)
def test_해소_공시를_알아본다(report_nm):
    assert dart.is_resolving(report_nm, dart.classify(report_nm)) is True


def test_아직_아니다는_해소가_아니다():
    """예고·기한 안내·미진행은 중립이다. 이걸 해소로 치면 진행 중인 상폐가 풀린다."""
    for nm in (
        "불성실공시법인지정예고              (공시번복)",
        "기타시장안내              (상장적격성 실질심사 대상결정 기한 안내)",
        "기타시장안내              (정기보고서 미제출 관련 상장폐지 절차 미진행)",
    ):
        assert dart.is_resolving(nm, dart.classify(nm)) is False, nm


def test_무관한_카테고리는_배제도_해소도_아니다():
    nm = "단일판매ㆍ공급계약체결"
    assert dart.is_disqualifying(nm, dart.classify(nm)) is False
    assert dart.is_resolving(nm, dart.classify(nm)) is False
