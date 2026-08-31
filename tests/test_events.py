"""급변 스캔 (ADR 0011).

여기서 지키는 것 셋.

1. **저장하지 않는다.** 일봉에서 매번 계산한다 — 규칙이 바뀌면 과거가 낡는 것을 막는다(11.7).
2. **두 축이 서로 다른 것을 잡는다.** 배수는 소형주, 절대 거래대금은 대형주.
3. **등락률은 ATR 배수다.** 고정 % 는 대형주를 통째로 놓친다.
"""

from __future__ import annotations

import json

import pytest

from data import events, store


def _seed(conn, code, *, closes, volumes, atr_pct, start="2026-08-01"):
    """일봉과 ATR 을 심는다. 날짜는 연속 영업일로 가정한다."""
    from datetime import date, timedelta

    d = date.fromisoformat(start)
    for close, vol in zip(closes, volumes, strict=True):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv "
            "(code,date,open,high,low,close,volume,halted,source,adjusted) "
            "VALUES (?,?,?,?,?,?,?,0,'t',1)",
            (code, d.isoformat(), close, close, close, close, vol),
        )
        d += timedelta(days=1)
    conn.execute(
        "INSERT OR REPLACE INTO indicators (code,date,payload) VALUES (?,?,?)",
        (code, d.isoformat(), json.dumps({"indicators": {"atr_pct": atr_pct}, "flows": {}})),
    )
    return d


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


def _last_day(conn, code):
    return conn.execute("SELECT MAX(date) FROM ohlcv WHERE code=?", (code,)).fetchone()[0]


# ── 1. 두 축 ────────────────────────────────────────────


def test_소형주는_배수로_잡힌다(db) -> None:
    """평소 조용하다가 거래가 터진 종목. 절대 거래대금은 작다."""
    _seed(
        db,
        "AAA",
        closes=[10_000] * 21 + [12_000],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    hits = events.scan(db, _last_day(db, "AAA"))
    assert [h.code for h in hits] == ["AAA"]
    assert events.REL in hits[0].axes
    assert hits[0].volume_multiple >= events.VOLUME_MULTIPLE


def test_대형주는_절대_거래대금으로_잡힌다(db) -> None:
    """실측 근거: SK이노베이션 배수 1.34배(98위) · 절대 거래대금 16위.

    분모가 크면 배수는 뜨지 않는다. 그래서 축이 둘이어야 한다.
    """
    # 큰 종목: 평소에도 거래대금이 크고 당일 배수는 1.3배뿐
    _seed(
        db,
        "BIG",
        closes=[100_000] * 21 + [104_400],
        volumes=[1_000_000] * 21 + [1_300_000],
        atr_pct=7.0,
    )
    # 작은 종목 여럿 — 절대 순위 경쟁자
    for i in range(5):
        _seed(db, f"S{i}", closes=[1_000] * 22, volumes=[1_000] * 22, atr_pct=8.0)

    hits = events.scan(db, _last_day(db, "BIG"), absolute_top_n=3)
    big = next(h for h in hits if h.code == "BIG")
    assert events.ABS in big.axes
    assert events.REL not in big.axes, "배수로는 안 걸려야 이 테스트가 의미 있다"
    assert big.volume_multiple < events.VOLUME_MULTIPLE


# ── 2. ATR 정규화 — 여기가 핵심이다 ─────────────────────


def test_등락률은_ATR_배수로_판정한다(db) -> None:
    """고정 5% 로 두면 대형주가 통째로 빠진다.

    실측: SK이노베이션 +4.38%(ATR 7.06% → 0.62배)는 5% 문턱에 못 걸렸고,
    원익홀딩스 -5.8%(ATR 11.1% → 0.52배)는 고정 5% 를 통과했다. 둘 다 틀린 판정이다.
    """
    # +4.4% 지만 ATR 7% 라 0.62배 → 잡혀야 한다
    _seed(
        db,
        "LOWVOL",
        closes=[100_000] * 21 + [104_400],
        volumes=[1_000_000] * 21 + [4_000_000],
        atr_pct=7.0,
    )
    # -5.8% 지만 ATR 11.1% 라 0.52배 → 빠져야 한다
    _seed(
        db,
        "HIVOL",
        closes=[100_000] * 21 + [94_200],
        volumes=[1_000_000] * 21 + [4_000_000],
        atr_pct=11.1,
    )

    codes = {h.code for h in events.scan(db, _last_day(db, "LOWVOL"), absolute_top_n=0)}
    assert "LOWVOL" in codes, "저변동 종목의 4.4% 는 큰 움직임이다"
    assert "HIVOL" not in codes, "고변동 종목의 5.8% 는 평범한 움직임이다"


def test_ATR_이_없으면_제외한다(db) -> None:
    """고정 % 로 물러서면 대형주·소형주에 같은 잣대를 대는 문제가 되돌아온다."""
    _seed(
        db,
        "AAA",
        closes=[10_000] * 21 + [12_000],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    db.execute("DELETE FROM indicators WHERE code='AAA'")
    assert events.scan(db, _last_day(db, "AAA")) == []


# ── 3. 방향과 미래 차단 ─────────────────────────────────


def test_급락도_사건이다(db) -> None:
    _seed(
        db,
        "DROP",
        closes=[10_000] * 21 + [8_000],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    hits = events.scan(db, _last_day(db, "DROP"))
    assert hits and hits[0].direction == "down"


def test_미래_봉을_보지_않는다(db) -> None:
    """리플레이에서 미래가 새면 11.9 의 사고가 재현된다."""
    end = _seed(
        db,
        "AAA",
        closes=[10_000] * 21 + [12_000],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    spike_day = _last_day(db, "AAA")
    # 급변 다음 날을 심는다 — 그 전날을 스캔하면 보이면 안 된다
    db.execute(
        "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted) "
        "VALUES ('AAA',?,1,1,1,99999,9999999,0,'t',1)",
        (end.isoformat(),),
    )
    hits = events.scan(db, spike_day)
    assert hits and hits[0].day == spike_day
    assert hits[0].change_pct == pytest.approx(20.0, abs=0.1), "미래 종가가 섞였다"


def test_저장하지_않는다(db) -> None:
    """규칙이 바뀌면 저장분이 낡는다(11.7). 테이블을 만들지 않는 것이 설계다."""
    _seed(
        db,
        "AAA",
        closes=[10_000] * 21 + [12_000],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    before = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    events.scan(db, _last_day(db, "AAA"))
    after = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert before == after
    assert "events" not in after and "event_hits" not in after


def test_임계를_바꾸면_결과가_바뀐다(db) -> None:
    """임계는 임시값이다 — 실험 7 이 정한다. 인자로 열려 있어야 실험을 돌릴 수 있다."""
    _seed(
        db,
        "AAA",
        closes=[10_000] * 21 + [10_700],
        volumes=[100_000] * 21 + [2_000_000],
        atr_pct=8.0,
    )
    day = _last_day(db, "AAA")
    # +7% / ATR 8% = 0.875배
    assert events.scan(db, day, min_change_atr=0.5) != [], "0.875 >= 0.5 이므로 잡혀야 한다"
    assert events.scan(db, day, min_change_atr=0.8) != [], "0.875 >= 0.8 이므로 잡혀야 한다"
    assert events.scan(db, day, min_change_atr=1.0) == [], "0.875 < 1.0 이므로 빠져야 한다"
