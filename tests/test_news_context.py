"""뉴스·공시 컨텍스트 — 데이터가 AI 에게 도달하는가 (ADR 0010).

여기서 지키는 것 셋.

1. **채널이 아니다.** 뉴스·공시가 후보를 만들지 않는다 — 유니버스 구성이 바뀌면 안 된다.
2. **전 arm 공통이다.** `derive_arm2()` 가 뉴스·공시를 건드리면 `Arm1 − Arm2` 가
   "브리핑 + 뉴스" 증분이 되어 F3 의 정의가 바뀐다.
3. **못 받으면 받은 척하지 않는다.** 장중 현재가와 같은 규칙이다.
"""

from __future__ import annotations

import types
from datetime import date, datetime

import pytest

from data import config as dcfg
from data.sources import naver_news as nn

# ── 1. 파싱과 군집 ──────────────────────────────────────


def _item(headline, at, source="매체", names=False):
    return nn.NewsItem(headline=headline, at=at, source=source, names_stock=names)


def test_같은_날_같은_사건은_한_건으로_묶인다() -> None:
    """묶지 않으면 상한(8건)이 한 사건으로 다 찬다 — 실측에서 14개 매체가 같은 건을 썼다."""
    items = [
        _item(
            "가온전선-DL이앤씨, 케이블 신공법 검증 완료…내년 본격 적용",
            "2026-08-25T08:30:00+09:00",
            "이데일리",
            True,
        ),
        _item(
            "가온전선·DL이앤씨, 배관·전선 하나로 묶었다...내년 본격 적용",
            "2026-08-25T12:32:00+09:00",
            "파이낸셜",
            True,
        ),
        _item(
            "가온전선-DL이앤씨, 케이블 신공법 검증…내년 적용",
            "2026-08-25T09:36:00+09:00",
            "한국경제TV",
            True,
        ),
    ]
    out = nn._cluster(items)
    assert len(out) == 1
    assert out[0].cluster_size == 3
    assert out[0].at.startswith("2026-08-25T08:30"), "대표는 가장 이른 기사다"


def test_날짜가_다르면_다른_사건이다() -> None:
    """제목이 비슷해도 날짜가 다르면 별개다 — 후속 보도를 원본과 합치지 않는다."""
    items = [
        _item(
            "가온전선-DL이앤씨, 케이블 신공법 검증 완료", "2026-08-25T09:00:00+09:00", names=True
        ),
        _item(
            "가온전선-DL이앤씨, 케이블 신공법 검증 완료", "2026-08-26T09:00:00+09:00", names=True
        ),
    ]
    assert len(nn._cluster(items)) == 2


def test_무관한_기사는_묶이지_않는다() -> None:
    """실측: 같은 사건 0.13~0.42 · 무관한 기사 0.00."""
    items = [
        _item(
            "가온전선-DL이앤씨, 케이블 신공법 검증 완료", "2026-08-25T09:00:00+09:00", names=True
        ),
        _item(
            "[애프터마켓 리뷰] 휴온스글로벌, 자회사 합병 철회에 상한가", "2026-08-25T10:00:00+09:00"
        ),
    ]
    assert len(nn._cluster(items)) == 2


def test_한_매체라도_종목명을_달면_그_종목_건이다() -> None:
    items = [
        _item("전선주 급등…전력망 수혜", "2026-08-25T09:00:00+09:00"),
        _item("전선주 급등…가온전선 전력망 수혜", "2026-08-25T09:10:00+09:00", names=True),
    ]
    out = nn._cluster(items)
    assert len(out) == 1 and out[0].names_stock


def test_종목명_달린_기사가_먼저_온다() -> None:
    """최신순으로만 자르면 상한이 전부 노이즈로 찬다 — 실측에서 상위 8건이 모두 무관했다."""
    items = [
        _item("코스피 마감시황", "2026-08-29T16:00:00+09:00"),
        _item("가온전선, 싱가포르 600억 수주", "2026-08-06T09:00:00+09:00", names=True),
    ]
    out = nn._cluster(items)
    assert out[0].names_stock, "더 오래됐어도 종목 기사가 먼저다"


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("2026.08.29 09:00", True), ("2026.08.29", False), ("", False), ("어제", False)],
)
def test_시각을_못_읽으면_추측하지_않는다(raw, ok) -> None:
    assert (nn._parse_at(raw) is not None) is ok


# ── 2. as_of 상한 ───────────────────────────────────────


class FakeHTTP:
    def __init__(self, html=""):
        self.html, self.calls = html, []

    def get(self, url, **kw):
        self.calls.append(kw.get("params", {}))
        return types.SimpleNamespace(status_code=200, text=self.html)

    def close(self):
        pass


_ROW_HTML = """
<td class="title"><a href="#">{t}</a></td><td class="info">{s}</td><td class="date">{d}</td>
"""


def _html(rows):
    return "".join(_ROW_HTML.format(t=t, s=s, d=d) for t, s, d in rows)


def test_미래_기사를_버린다() -> None:
    """11.9 에서 브리핑 채널이 미래 정보를 읽었던 사고가 그대로 재현될 수 있다."""
    html = _html(
        [
            ("과거 기사 가온전선", "매체", "2026.08.28 09:00"),
            ("미래 기사 가온전선", "매체", "2026.08.31 23:00"),
        ]
    )
    got, failed = nn.for_universe(
        [("000500", "가온전선")],
        as_of_iso="2026-08-31T09:00:00+09:00",
        client=FakeHTTP(html),
        pages=1,
    )
    heads = [n["headline"] for n in got["000500"]]
    assert any("과거" in h for h in heads)
    assert not any("미래" in h for h in heads), "as_of 이후 기사가 샜다"
    assert not failed


def test_실패한_종목을_사유와_함께_돌려준다() -> None:
    class Boom(FakeHTTP):
        def get(self, url, **kw):
            return types.SimpleNamespace(status_code=503, text="")

    got, failed = nn.for_universe(
        [("000500", "가온전선")], as_of_iso="2026-08-31T09:00:00+09:00", client=Boom(), pages=1
    )
    assert got == {} and "503" in failed["000500"]


# ── 3. 팩 배선 ──────────────────────────────────────────


def test_뉴스는_기본이_꺼져_있다() -> None:
    """라이브러리가 부르는 것만으로 외부 호출이 나가면 테스트가 느려지고 불안정해진다."""
    import inspect

    from decision import pack

    assert inspect.signature(pack.build).parameters["with_news"].default is False


def test_뉴스를_못_받으면_받은_척하지_않는다() -> None:
    from decision import pack

    items = [{"code": "000500", "name": "가온전선"}]
    dq: dict = {"warnings": []}

    class Dead:
        def get(self, *a, **k):
            raise RuntimeError("네트워크 없음")

        def close(self):
            pass

    pack.attach_news(items, dq, now=datetime(2026, 8, 31, 9, tzinfo=dcfg.KST), client=Dead())
    assert dq["news_as_of"] is None
    assert any("당일 이슈" in w for w in dq["warnings"])


def test_공시가_팩에_실린다(tmp_path) -> None:
    """예전에는 거르는 데만 쓰고 싣지 않아 AI 가 IR·공급계약을 한 건도 볼 수 없었다."""
    from data import store
    from decision import pack

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        for dt, nm, bad in (
            ("20260828", "기업설명회(IR)개최(안내공시)", 0),
            ("20260827", "[기재정정]타인에대한채무보증결정", 0),
            ("20260101", "오래된공시", 0),  # 조회 창 밖
        ):
            conn.execute(
                "INSERT INTO disclosures (rcept_no,corp_code,code,corp_name,report_nm,"
                "rcept_dt,filer,category,material,disqualifying,resolving,url) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,0,'')",
                (f"r{dt}{nm[:3]}", "c006360", "006360", "GS건설", nm, dt, "GS건설", "기타", bad),
            )
        items = [{"code": "006360", "name": "GS건설"}]
        n = pack.attach_disclosures(conn, items, date(2026, 8, 31))

    assert n == 2, "조회 창(14일) 밖 공시가 섞였다"
    names = [d["report_nm"] for d in items[0]["disclosures"]]
    assert any("기업설명회" in x for x in names)
    assert items[0]["disclosures"][0]["at"] == "2026-08-28", "최신순이 아니다"


# ── 4. arm 분리 — 여기가 핵심이다 ───────────────────────


def test_arm2_는_뉴스_공시를_건드리지_않는다() -> None:
    """뉴스를 Arm 2 에서 빼면 `Arm1 − Arm2` 가 '브리핑 + 뉴스' 증분이 되어 F3 가 바뀐다.

    뉴스·공시는 봉투이고 브리핑이 측정 대상이다 (ADR 0010 결정 2).
    """
    from decision import engine

    news = [
        {
            "headline": "h",
            "at": "2026-08-25T09:00:00+09:00",
            "source": "s",
            "names_stock": True,
            "cluster_size": 3,
        }
    ]
    disc = [{"at": "2026-08-28", "report_nm": "IR", "disqualifying": False}]
    pack = {
        "universe": [
            {
                "code": "005930",
                "channels": ["momentum"],
                "screen_reasons": [],
                "news": news,
                "disclosures": disc,
            },
            {
                "code": "035720",
                "channels": ["briefing"],
                "screen_reasons": [],
                "news": news,
                "disclosures": disc,
            },
        ],
        "briefings": [{"briefing_id": "b1"}],
        "data_quality": {"warnings": []},
    }
    out = engine.derive_arm2(pack)

    assert [u["code"] for u in out["universe"]] == ["005930"], "브리핑 전용 종목은 빠져야 한다"
    assert out["universe"][0]["news"] == news, "뉴스가 arm 2 에서 변형됐다"
    assert out["universe"][0]["disclosures"] == disc, "공시가 arm 2 에서 변형됐다"
