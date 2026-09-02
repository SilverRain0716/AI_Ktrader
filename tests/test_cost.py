"""비용 계산. **모르는 것을 0 으로 두지 않는다.**"""

from __future__ import annotations

import sqlite3

import pytest

from data import store
from ops import cost


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    return conn


def _dec(conn, did, *, arm=1, pack="P", model="gpt-5.6", i=1000, o=100, cached=None, kind="live"):
    conn.execute(
        "INSERT INTO decisions (decision_id,run_kind,attempt,pack_id,pack_sha256,arm,cycle,"
        "generated_at,valid_until,render_version,status,model,input_tokens,output_tokens,"
        "cached_input_tokens) VALUES (?,?,1,?,'s',?,'premarket','2026-09-02T08:00:00+09:00',"
        "'2026-09-02T13:00:00+09:00','r1','ok',?,?,?,?)",
        (did, kind, pack, arm, model, i, o, cached),
    )


def test_별칭을_실제_모델로_푼다():
    """`gpt-5.6` 로 요청하면 응답 model 은 `gpt-5.6-sol` 이다 (API 실측)."""
    u = cost.Usage("gpt-5.6", 1, 1_000_000, 0, 0)
    assert u.usd() == pytest.approx(4.00)


def test_캐시된_몫은_싸게_친다():
    u = cost.Usage("gpt-5.6-sol", 1, 1_000_000, 0, 1_000_000)
    assert u.usd() == pytest.approx(0.40)
    half = cost.Usage("gpt-5.6-sol", 1, 1_000_000, 0, 500_000)
    assert half.usd() == pytest.approx(0.5 * 4.00 + 0.5 * 0.40)


def test_모르는_모델은_금액을_내지_않는다():
    """추측한 단가로 계산한 숫자는 **없느니만 못하다.**"""
    assert cost.Usage("무슨모델", 1, 1_000_000, 1_000_000, 0).usd() is None


def test_캐시를_재지_못한_것과_안_먹은_것은_다르다():
    assert cost.Usage("gpt-5.6-sol", 1, 1000, 0, None).cache_hit_pct is None
    assert cost.Usage("gpt-5.6-sol", 1, 1000, 0, 0).cache_hit_pct == 0.0


def test_한_호출이라도_못_쟀으면_합계도_못_잰_것이다(db):
    """일부만 더해 비율을 내면 **실제보다 낮게 나온다.**"""
    _dec(db, "A-a1", arm=1, i=1000, cached=800)
    _dec(db, "A-a2", arm=2, i=1000, cached=None)
    ((_, u),) = cost.by_cycle(db)
    assert u.cached_tokens is None
    assert u.cache_hit_pct is None


def test_둘_다_쟀으면_합산한다(db):
    _dec(db, "B-a1", arm=1, i=1000, cached=800)
    _dec(db, "B-a2", arm=2, i=1000, cached=200)
    ((_, u),) = cost.by_cycle(db)
    assert (u.cached_tokens, u.cache_hit_pct) == (1000, 50.0)


def test_실험_결정은_세지_않는다(db):
    """집행되지 않는 판단의 비용을 운영 비용과 섞으면 예산이 흐려진다."""
    _dec(db, "C-a1", kind="experiment")
    assert cost.by_cycle(db) == []
    assert cost.total(db) == []


def test_설정_파일이_내장표를_이긴다(tmp_path, monkeypatch):
    """단가는 우리가 정하는 값이 아니고 바뀐다."""
    f = tmp_path / "p.json"
    f.write_text('{"gpt-5.6-sol": {"input": 9.0, "cached_input": 1.0, "output": 9.0}}')
    monkeypatch.setattr(cost, "PRICES_PATH", f)
    assert cost.Usage("gpt-5.6", 1, 1_000_000, 0, 0).usd() == pytest.approx(9.0)
