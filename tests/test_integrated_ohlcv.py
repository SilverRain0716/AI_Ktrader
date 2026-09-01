"""통합 거래소(KRX+NXT) 일봉.

네이버 일봉은 KRX 만 담는다. 실측(2026-09-01, 24종목 표본):
**우리 DB / 통합 = 중앙 75% · 최소 35% · 최대 100%**, NXT 비중이 종목마다 **0~65%** 다.

단순 배율이 아니라 종목마다 갈리므로 **거래대금 비교가 통째로 왜곡된다** —
원칙 5(거래대금 방향)와 급변 스캔(ADR 0011)이 전부 그 위에 서 있다.
"""

from __future__ import annotations

import pytest

from data.sources import kiwoom


class _FakeChart:
    """`ka10081` 응답만 흉내낸다. **네트워크를 타지 않는다.**"""

    def __init__(self, rows, expect=None):
        self.rows, self.expect, self.seen = rows, expect, []

    def post(self, tr, path, body):
        self.seen.append((tr, body))
        if self.expect:
            assert body["stk_cd"] == self.expect, body["stk_cd"]
        return {"return_code": 0, "stk_dt_pole_chart_qry": self.rows}


def _row(dt="20260831", o=256000, h=260000, low=246000, c=258000, q=28001732, v=7109091):
    return {
        "dt": dt,
        "open_pric": str(o),
        "high_pric": str(h),
        "low_pric": str(low),
        "cur_prc": str(c),
        "trde_qty": str(q),
        "trde_prica": str(v),
    }


def _client(rows, expect=None):
    c = kiwoom.KiwoomClient.__new__(kiwoom.KiwoomClient)
    fake = _FakeChart(rows, expect)
    c.post = fake.post
    return c, fake


def test_통합은_AL_접미사를_쓴다():
    c, fake = _client([_row()], expect="005930_AL")
    c.daily_chart("005930", base_dt="20260831")
    assert fake.seen[0][0] == kiwoom.DAILY_CHART_TR


def test_KRX_는_접미사가_없다():
    c, _fake = _client([_row()], expect="005930")
    c.daily_chart("005930", base_dt="20260831", venue="KRX")


def test_기본은_수정주가다():
    """네이버와 같은 축척이어야 섞어도 안전하다.

    실측: 가온전선이 원본가와는 224/267 불일치인데 **수정주가와는 0/267 일치**한다.
    """
    c, fake = _client([_row()])
    c.daily_chart("005930", base_dt="20260831")
    assert fake.seen[0][1]["upd_stkpc_tp"] == "1"
    c.daily_chart("005930", base_dt="20260831", adjusted=False)
    assert fake.seen[1][1]["upd_stkpc_tp"] == "0"


def test_날짜를_ISO_로_바꾼다():
    c, _ = _client([_row(dt="20260831")])
    assert c.daily_chart("005930", base_dt="20260831")[0]["date"] == "2026-08-31"


def test_거래대금은_백만원에서_억원으로():
    """접미사가 곧 단위다 — 저장소 관례는 `_eok_krw`(억원)."""
    c, _ = _client([_row(v=7109091)])
    assert c.daily_chart("005930", base_dt="20260831")[0]["value_eok"] == pytest.approx(71090.91)


def test_0값_행을_버린다():
    """거래정지일에 0 이 섞이면 ATR·볼린저가 오염된다 (데이터 계층 함정)."""
    c, _ = _client([_row(), _row(dt="20260828", o=0, h=0, low=0, c=0, q=0)])
    got = c.daily_chart("005930", base_dt="20260831")
    assert [b["date"] for b in got] == ["2026-08-31"]


def test_부호_접두사를_뗀다():
    """키움의 `-257000` 은 음수가 아니라 하락 표시다 (데이터 계층 함정)."""
    c, _ = _client([_row(c="-258000", o="+256000")])
    b = c.daily_chart("005930", base_dt="20260831")[0]
    assert b["close"] == 258000 and b["open"] == 256000


def test_통합_적재가_지수를_건드리지_않는다():
    """`ka10081` 이 지수를 1건만 준다 — 지수는 네이버를 유지해야 한다."""
    import inspect

    from data import pipeline as dp

    src = inspect.getsource(dp.task_ohlcv_integrated)
    assert "listing" in src, "상장 목록 조인이 없으면 KOSPI·KOSDAQ 이 섞인다"


def test_통합_적재_뒤_지표_재계산을_알린다():
    """종가·거래량이 바뀌었는데 지표가 그대로면 조용히 어긋난다."""
    import inspect

    from data import pipeline as dp

    src = inspect.getsource(dp.task_ohlcv_integrated)
    assert "indicators" in src and "다시 계산" in src


def test_거래량_0인_행을_버린다():
    """**장 시작 전에 돌리면 당일 행이 거래량 0 으로 온다.**

    실측(2026-09-01 06:15): 668종목 전부 OHLC 가 같고 거래량 0 인 09-01 행이 들어왔다.
    그것을 halted=0 으로 저장하면 "정지 아닌데 거래량 0" 이라는 모순이 남는다.
    """
    c, _ = _client([_row(), _row(dt="20260901", q=0)])
    got = c.daily_chart("005930", base_dt="20260901")
    assert [b["date"] for b in got] == ["2026-08-31"]
