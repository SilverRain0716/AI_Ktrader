"""브리핑 파서 테스트.

픽스처는 전부 실제 브리핑에서 그대로 가져온 표기다. 형식이 5가지로 갈리므로
각 변형을 하나씩 고정해 둔다 — 브리핑 생성 프롬프트가 바뀌면 여기가 먼저 깨져야 한다.
"""

from __future__ import annotations

import sqlite3

import pytest

from briefing import config, parser
from data import store

# ── 관점 파싱 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "line,stance,conf,inherited,note",
    [
        ("· 관점: 주목 / 확신도 중상", "주목", "중상", False, None),
        ("· 관점: 경계 / 확신도: 중상", "경계", "중상", False, None),  # 2026-08-17 이후 표기
        ("· 관점: 경계(승계) / 확신도 중", "경계", "중", True, None),
        ("· 관점: 회피 / 확신도 중상(발표 확정)", "회피", "중상", False, "발표 확정"),
        ("[1] SK하이닉스(000660) — 관점: 조건부 / 확신도 중", "조건부", "중", False, None),
        (" 관점: 주목 / 확신도 중상 / 틀리는 조건: X / 확인: Y", "주목", "중상", False, None),
    ],
)
def test_parse_stance_변형(line, stance, conf, inherited, note):
    r = parser.parse_stance(line)
    assert r["stance"] == stance
    assert r["confidence"] == conf
    assert r["stance_inherited"] is inherited
    assert r["confidence_note"] == note


def test_확신도가_없어도_관점만으로_성립한다():
    """2026-08-17 0800 브리핑에 실제로 확신도가 빠져 있었다. 없는 값을 지어내지 않는다."""
    r = parser.parse_stance("[1] SK하이닉스(000660) — 관점: 경계")
    assert r["stance"] == "경계"
    assert r["confidence"] is None


def test_관점이_없으면_None():
    assert parser.parse_stance("· 촉매: 자사주 소각 개시") is None
    assert parser.parse_stance("SK하이닉스 주목(중상) → +12.73%. 관점 적중") is None


# ── 헤더에서 종목 식별 ───────────────────────────────────


@pytest.mark.parametrize(
    "header,name,code,symbol",
    [
        ("SK하이닉스(000660) 169만1,000원 +12.73%", "SK하이닉스", "000660", None),
        ("알테오젠(196170) +11.86% (코스닥 대장)", "알테오젠", "196170", None),
        ("리가켐바이오 (141080) +15.2%", "리가켐바이오", "141080", None),
        ("모더나(MRNA) 종가 +약100%", "모더나", None, "MRNA"),
        ("월마트(WMT) — 실적 발표됨, 프리마켓 약 -5.7%", "월마트", None, "WMT"),
        ("SK하이닉스(000660) — 관점: 조건부 / 확신도 중", "SK하이닉스", "000660", None),
    ],
)
def test_parse_header(header, name, code, symbol):
    r = parser.parse_header(header)
    assert r["name"] == name
    assert r["code"] == code
    assert r["symbol"] == symbol


def test_괄호가_등락률이면_코드로_오인하지_않는다():
    """'삼성전자 (+8%대)' 의 괄호는 종목코드가 아니다."""
    r = parser.parse_header("삼성전자 (+8%대) — 반도체 낙폭과대 반발")
    assert r["name"] == "삼성전자"
    assert r["code"] is None and r["symbol"] is None


# ── 근거 분해 ────────────────────────────────────────────


def test_split_reasons_원문자():
    r = parser.split_reasons("①40조 소각은 확정 사실 ②외국인 순매수 전환")
    assert r == ["40조 소각은 확정 사실", "외국인 순매수 전환"]


def test_split_reasons_숫자괄호():
    r = parser.split_reasons("1)임상은 실질 성공이나 과열 2)매출화까지 수년")
    assert len(r) == 2 and r[0].startswith("임상은")


def test_split_reasons_나열이_아니면_통째로():
    assert parser.split_reasons("단일 근거 문장") == ["단일 근거 문장"]


# ── 브리핑 전체 파싱 (실제 형식) ─────────────────────────

_KR_CLOSE_DEEP = """## 18:00 한국 장마감 종합

📌 한국 장마감 종합 (1/2) — 2026-08-20(목)

한 줄 요약: 코스피 폭반등. 외국인 순매수 전환이 핵심.

[마감 지수]
· 코스피 6,852.58 (+5.89%)

[주요 공시] DART 8/20
· SK하이닉스 자기주식 취득·소각 개시
  https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260819000254

[종목 심층분석] (촉매 확인 1종목)

[1] SK하이닉스(000660) 169만1,000원 +12.73%
· 촉매: 40조 자사주 취득·소각 오늘 실제 개시(DART 8/19 확정).
· 관점: 주목 / 확신도 중상
· 근거: ①40조 소각·취득 오늘 개시는 확정 사실 ②외국인 순매수 전환
· 틀리는 조건: 오늘밤 미 SOX 하락 마감 / 확인: 오늘밤 SOX, 8/26 엔비디아
"""


def test_kr_close_deep_파싱():
    p = parser.parse("2026-08-20", "1800-kr-close-deep", _KR_CLOSE_DEEP)
    assert p.kind == "kr-close-deep"
    assert p.market == "KR"
    assert p.published_at.startswith("2026-08-20T18:00")
    assert p.briefing_id == "2026-08-20-1800-kr-close-deep"
    assert p.summary.startswith("코스피 폭반등")
    assert "마감 지수" in p.sections

    assert len(p.views) == 1
    v = p.views[0]
    assert (v["code"], v["stance"], v["confidence"]) == ("000660", "주목", "중상")
    assert len(v["reasons"]) == 2
    assert v["invalidation"] == "오늘밤 미 SOX 하락 마감"
    assert "SOX" in v["check_at"]
    assert v["catalyst"]["summary"].startswith("40조")

    assert p.disclosures and p.disclosures[0]["rcept_no"] == "20260819000254"


_KR_PRECLOSE = """## 14:50 장마감 전 점검

🔔 장마감 전 점검 — 2026-08-20(목) 14:50 (잠정) (1/2)

[장중 리더] (오전 확인치)
1. SK하이닉스 (+12%대, 170만원선 근접) — 40조 자사주 소각 오늘 개시(DART).
 관점: 주목 / 확신도 중상 / 틀리는 조건: 오늘 마감까지 외국인이 하이닉스를 순매도로 전환 / 확인: 오후 외국인 순매수 전환
"""


def test_kr_preclose_코드없는_변형():
    """1450 브리핑은 종목코드를 적지 않는다. 버리지 않고 경고와 함께 보존한다."""
    p = parser.parse("2026-08-20", "1450-kr-preclose", _KR_PRECLOSE)
    assert len(p.views) == 1
    v = p.views[0]
    assert v["name"] == "SK하이닉스"
    assert v["code"] is None
    assert v["stance"] == "주목"
    assert v["invalidation"].startswith("오늘 마감까지")
    assert any("종목코드 없음" in w for w in p.parse_warnings)


_US_CLOSE = """## 05:00 미국 장마감 브리핑

한 줄 요약: 다우·S&P 3거래일 만에 반등.

[종목 심층분석]

1) 모더나(MRNA) 종가 +약100%
· 촉매: mRNA 흑색종 백신 3상 성공(8/19).
· 관점: 경계 / 확신도 중
  - 근거 1)임상은 실질 성공이나 하루 +100%는 과열 2)매출화까지 수년
  - 틀리는 조건: 상세 데이터가 시장 기대를 밑도는 것으로 확인되는 경우
  - 확인: 학회 상세 데이터·규제 절차 일정
· 한국 연결: 에스티팜·아이진
· 출처: https://cnbc.com/moderna
"""


def test_us_close_티커와_한국연결():
    p = parser.parse("2026-08-20", "0500-us-close", _US_CLOSE)
    assert p.market == "US"
    v = p.views[0]
    assert v["symbol"] == "MRNA" and v["code"] is None
    assert v["market"] == "US"
    assert len(v["reasons"]) == 2
    assert v["kr_links"] == ["에스티팜", "아이진"]
    assert any("cnbc.com" in s for s in v["sources"])


def test_관점체계_도입_이전_브리핑은_정상으로_처리된다():
    """2026-08-05 이전에는 관점 자체가 없었다. 파싱 실패로 오인하면 안 된다."""
    old = "## 17:00 한국 장마감 종목 심층분석\n\n[1] 리가켐바이오 (141080) +15.2%\n- 오늘: 섹터 반등.\n"
    p = parser.parse("2026-08-04", "1700-kr-close-deep", old)
    assert p.views == []
    assert any("관점 체계 도입" in w for w in p.parse_warnings)


def test_알_수_없는_종류는_예외():
    with pytest.raises(ValueError, match="알 수 없는"):
        parser.parse("2026-08-20", "9999-unknown", "## x")


def test_빈_원문():
    p = parser.parse("2026-08-20", "1800-kr-close-deep", "")
    assert p.views == []
    assert "원문이 비어 있다" in p.parse_warnings


# ── 저장소 왕복 ──────────────────────────────────────────


def test_저장_후_조회():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    p = parser.parse("2026-08-20", "1800-kr-close-deep", _KR_CLOSE_DEEP)
    n = store.upsert_briefing(
        conn, p.to_dict(), stem="1800-kr-close-deep", ingested_at="2026-08-21T00:00:00+09:00"
    )
    assert n == 1

    # 멱등: 두 번 넣어도 관점이 중복되지 않는다
    store.upsert_briefing(
        conn, p.to_dict(), stem="1800-kr-close-deep", ingested_at="2026-08-21T00:00:00+09:00"
    )
    assert conn.execute("SELECT COUNT(*) FROM briefing_views").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM briefings").fetchone()[0] == 1

    from datetime import date

    df = store.load_views(conn, start=date(2026, 8, 20), end=date(2026, 8, 20), stances=["주목"])
    assert len(df) == 1 and df.iloc[0]["code"] == "000660"


def test_name_to_code_매핑():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO listing (code,name,market,is_preferred,is_spac,updated_at) "
        "VALUES ('000660','SK하이닉스','KOSPI',0,0,'x')"
    )
    m = store.name_to_code_map(conn)
    assert m["SK하이닉스"] == "000660"


def test_모든_stem이_KINDS에_있다():
    """실제 저장소에 존재하는 13종. 새 종류가 생기면 이 테스트가 먼저 깨져야 한다."""
    assert len(config.KINDS) == 13
    for _stem, (kind, hhmm, market) in config.KINDS.items():
        assert market in ("KR", "US", "GLOBAL")
        assert len(hhmm) == 5 and hhmm[2] == ":"
        assert kind.replace("-", "").isalpha()


def test_확신도_중하를_중으로_잘라먹지_않는다():
    """실측 19회 쓰인 값이다. 정규식 대안 순서가 잘못되면 '중'으로 조용히 오기록된다."""
    r = parser.parse_stance("· 관점: 경계 / 확신도 중하")
    assert r["confidence"] == "중하"


def test_정의되지_않은_확신도는_null_과_경고():
    """'낮게', '보수적' 같은 자유 서술이 실제로 있었다. 지어내지 말고 경고로 드러낸다."""
    block = "[1] 테스트(005930) — 관점: 경계 / 확신도 낮게\n· 근거: ①a ②b\n· 틀리는 조건: x\n"
    p = parser.parse("2026-08-20", "1800-kr-close-deep", "## t\n\n[종목]\n" + block)
    assert p.views[0]["confidence"] is None
    assert any("확신도 표기를 인식하지 못함" in w for w in p.parse_warnings)


@pytest.mark.parametrize(
    "line,stance,conf",
    [
        ("1. 삼성전자(005930) 281,500원 +3.78% — 주목/확신도 중상", "주목", "중상"),
        ("3. 카카오(035720) 매매거래정지 — 조건부/확신도 중", "조건부", "중"),
        ("4. 알테오젠(196170) 코스닥 급락 동반 하락 — 경계/확신도 중", "경계", "중"),
    ],
)
def test_라벨_없이_헤더에_붙은_관점(line, stance, conf):
    """2026-08-21부터 '관점:' 라벨이 사라진 형식이 등장했다. 3건이 통째로 누락됐었다."""
    r = parser.parse_stance(line)
    assert r["stance"] == stance and r["confidence"] == conf


def test_복기_문장을_관점으로_오인하지_않는다():
    """'SK하이닉스 주목(중상) → +12.73%' 는 복기이지 새 관점이 아니다."""
    assert parser.parse_stance("1. SK하이닉스 주목(중상) → +12.73%. 관점 적중") is None
