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


# ── 라벨 없는 서술문 근거 (점검 2026-08-22 결함 6) ───────
# kr-preclose·us-premarket 은 `근거:` 라벨을 쓰지 않고 종목명 뒤 서술문에 근거를 담는다.
# 라벨만 보면 관점의 40%가 규칙 미달로 집계되는데, 파서가 그 줄을 안 읽었을 뿐이었다.
# 아래 헤더는 전부 실제 브리핑에서 그대로 가져왔다.


@pytest.mark.parametrize(
    "header,expected",
    [
        # '+' 가 접속사이면서 등락률 부호이기도 하다. 뒤에 숫자가 오면 자르지 않는다.
        (
            "1. SK하이닉스(000660) +3%대 — 40조 자사주 취득·소각 매 거래일 실매입(승계)"
            "+간밤 SOX +0.53%·마이크론 강세.",
            ["40조 자사주 취득·소각 매 거래일 실매입(승계)", "간밤 SOX +0.53%·마이크론 강세."],
        ),
        # 'DART 당일 공시 없음' 은 촉매의 부재를 알리는 꼬리말이지 상승 근거가 아니다.
        (
            "1. 알테오젠 (+15.57%) — 블랙록 지분 5.03% 확대(7/31 공시) 재부각 + "
            "코스닥 바이오 순환매·외국인/기관 쌍끌이. DART 당일 공시 없음",
            ["블랙록 지분 5.03% 확대(7/31 공시) 재부각", "코스닥 바이오 순환매·외국인/기관 쌍끌이"],
        ),
        # 자릿점 쉼표('+3,440억')는 근거 구분자가 아니다.
        (
            "1. 삼성전자 (+5.33%) — 코스피 외국인 순매수 급확대(오전 +440억→오후 +3,440억)에 "
            "전기전자 견인, 애플發 메모리 원가 상승 우호 read-through 승계",
            [
                "코스피 외국인 순매수 급확대(오전 +440억→오후 +3,440억)에 전기전자 견인",
                "애플發 메모리 원가 상승 우호 read-through 승계",
            ],
        ),
    ],
)
def test_서술문에서_근거를_뽑는다(header, expected):
    assert parser.reasons_from_narrative(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        "2. 로켓랩(RKLB) — 프리마켓 +9.46%",
        "3. 슈퍼마이크로(SMCI) — 프리마켓 +5.96%",
        "1. 코어위브(CRWV) — 프리마켓 +17~18% (106달러 부근)",
        "1. SK하이닉스(000660) +1.03%(166.2만원)",
    ],
)
def test_등락률만_있으면_근거가_아니다(header):
    """움직였다는 사실은 왜 움직였는지가 아니다.

    이걸 근거로 세면 '근거 2개 이상' 경고만 사라지고 입력 품질은 그대로다 —
    가드가 틀린 이유로 통과하는 바로 그 패턴이다."""
    assert parser.reasons_from_narrative(header) == []


def test_구분선이_없으면_뽑지_않는다():
    assert parser.reasons_from_narrative("1. SK하이닉스(000660) +1.03%") == []


def test_라벨_근거가_있으면_서술문을_쓰지_않는다():
    """라벨이 명시돼 있으면 그것이 브리핑이 의도한 근거다. 서술문으로 덮어쓰지 않는다."""
    text = "\n".join(
        [
            "## 14:50 장마감 전 점검",
            "### 장중 리더",
            "1. 삼성전자(005930) +3% — 서술문 근거 하나 + 서술문 근거 둘",
            "· 근거: ① 라벨 근거 하나 ② 라벨 근거 둘 ③ 라벨 근거 셋",
            "· 관점: 주목 / 확신도 중상",
        ]
    )
    p = parser.parse("2026-08-21", "1450-kr-preclose", text)
    assert p.views[0]["reasons"] == ["라벨 근거 하나", "라벨 근거 둘", "라벨 근거 셋"]


def test_라벨이_없으면_서술문에서_보충한다():
    text = "\n".join(
        [
            "## 14:50 장마감 전 점검",
            "### 장중 리더",
            "1. 삼성전자(005930) +3% — 외국인 순매수 급확대 + 반도체 업종 강세",
            "· 관점: 주목 / 확신도 중상",
        ]
    )
    p = parser.parse("2026-08-21", "1450-kr-preclose", text)
    assert p.views[0]["reasons"] == ["외국인 순매수 급확대", "반도체 업종 강세"]
    assert not any("근거" in w for w in p.parse_warnings), p.parse_warnings


# ── 파생값이 낡는 문제 (점검 2026-08-22 결함 5·6) ────────
# parse_warnings 는 파싱 시점에 기록되는데, 코드 매핑과 파서 규칙 변경은 그 뒤에 온다.
# 걷어내지 않으면 이미 해결된 경고가 영원히 남는다 — 실측에서 '종목코드 없음' 이
# 38건으로 집계됐지만 실제 미매핑은 2건이었다.

_라벨없는_브리핑 = "\n".join(
    [
        "## 14:50 장마감 전 점검",
        "### 장중 리더",
        "1. 알테오젠 (+15.57%) — 블랙록 지분 확대 재부각 + 코스닥 바이오 순환매",
        "· 관점: 주목 / 확신도 중상",
        "· 틀리는 조건: 순환매가 하루로 끝나는 경우",
    ]
)


def _브리핑_db(text=_라벨없는_브리핑):
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    conn.execute(
        "INSERT INTO listing (code,name,market,is_preferred,is_spac,updated_at) "
        "VALUES ('196170','알테오젠','KOSDAQ',0,0,'x')"
    )
    p = parser.parse("2026-08-21", "1450-kr-preclose", text)
    store.upsert_briefing(
        conn, p.to_dict(), stem="1450-kr-preclose", ingested_at="2026-08-22T00:00:00+09:00"
    )
    return conn


def _경고(conn):
    import json

    return json.loads(conn.execute("SELECT parse_warnings FROM briefings").fetchone()[0])


def test_코드가_채워지면_종목코드_경고를_걷어낸다():
    from briefing import pipeline

    conn = _브리핑_db()
    assert any("종목코드 없음" in w for w in _경고(conn))

    pipeline.task_map_codes(conn)

    assert conn.execute("SELECT code FROM briefing_views").fetchone()[0] == "196170"
    assert not any("종목코드 없음" in w for w in _경고(conn)), _경고(conn)


def test_매핑에_실패하면_경고는_남는다():
    """해결되지 않은 경고까지 지우면 통계가 반대로 거짓말한다."""
    from briefing import pipeline

    conn = _브리핑_db()
    conn.execute("DELETE FROM listing")
    pipeline.task_map_codes(conn)
    assert any("종목코드 없음" in w for w in _경고(conn))


def test_재판정이_근거를_보충하고_경고를_지운다():
    from briefing import pipeline

    conn = _브리핑_db()
    # 옛 규칙(라벨만 인식)으로 적재된 상태를 만든다
    conn.execute("UPDATE briefing_views SET reasons='[]'")
    conn.execute(
        "UPDATE briefings SET parse_warnings=?",
        ('["알테오젠: 근거 0개 (브리핑 규칙은 2개 이상)"]',),
    )

    assert pipeline.task_reparse(conn) == 1

    import json

    assert len(json.loads(conn.execute("SELECT reasons FROM briefing_views").fetchone()[0])) == 2
    assert _경고(conn) == []


def test_재판정은_라벨_근거를_건드리지_않는다():
    from briefing import pipeline

    conn = _브리핑_db(
        "\n".join(
            [
                "## 14:50 장마감 전 점검",
                "### 장중 리더",
                "1. 알테오젠 (+15.57%) — 서술문 하나 + 서술문 둘",
                "· 근거: ① 라벨 하나 ② 라벨 둘",
                "· 관점: 주목 / 확신도 중상",
            ]
        )
    )
    assert pipeline.task_reparse(conn) == 0

    import json

    assert json.loads(conn.execute("SELECT reasons FROM briefing_views").fetchone()[0]) == [
        "라벨 하나",
        "라벨 둘",
    ]


def test_근거가_여전히_부족하면_실제_개수로_다시_쓴다():
    """경고를 지우는 게 목적이 아니라 실제 상태를 반영하는 게 목적이다."""
    from briefing import pipeline

    conn = _브리핑_db(
        "\n".join(
            [
                "## 14:50 장마감 전 점검",
                "### 장중 리더",
                "1. 알테오젠 (+15.57%) — 코스닥 바이오 순환매",
                "· 관점: 주목 / 확신도 중상",
            ]
        )
    )
    conn.execute("UPDATE briefing_views SET reasons='[]'")
    conn.execute(
        "UPDATE briefings SET parse_warnings=?",
        ('["알테오젠: 근거 0개 (브리핑 규칙은 2개 이상)"]',),
    )
    pipeline.task_reparse(conn)
    assert _경고(conn) == ["알테오젠: 근거 1개 (브리핑 규칙은 2개 이상)"]


# ── 일일 배치 배선 ──────────────────────────────────────


def test_daily_배치가_브리핑을_동기화한다() -> None:
    """**이것이 빠져 있어서 브리핑이 사흘 낡았다**(2026-09-01 발견).

    일봉·수급·공시·지표는 돌았지만 브리핑 동기화는 별도 명령이라 아무도 안 돌렸다.
    그 사이 팩의 briefing 채널이 비어 유니버스가 2채널로만 구성됐고,
    **Arm 1·2 의 입력이 같아져 F3 를 잴 수 없었다.**
    """
    import inspect

    from data import pipeline as dp

    src = inspect.getsource(dp.main)
    daily = src[src.index('== "daily"') :]
    daily = daily[: daily.index("task_status")]
    assert "task_briefings" in daily, "daily 배치에서 브리핑 동기화가 빠졌다"


def test_브리핑_동기화_실패가_배치를_멈추지_않는다(tmp_path, monkeypatch, caplog) -> None:
    """브리핑은 외부 저장소다. 못 받아도 일봉이 들어오는 것이 더 중요하다.

    다만 **조용히 넘어가면 안 된다** — 낡은 브리핑으로 도는 것을 아무도 모르게 된다.
    """
    import logging

    from briefing import pipeline as bp
    from data import pipeline as dp

    def boom(*a, **k):
        raise RuntimeError("gitlab 접속 불가")

    monkeypatch.setattr(bp, "task_sync", boom)
    with caplog.at_level(logging.ERROR):
        dp.task_briefings(None, days=3)  # 예외가 밖으로 나오면 안 된다
    assert any("브리핑 동기화 실패" in r.message for r in caplog.records)
    assert any("F3" in r.message for r in caplog.records), "무엇을 잃는지 말하지 않았다"
