"""순위대로 담는 규칙. **자르는 곳은 selection 하나뿐이다.**"""

from __future__ import annotations

from decision import selection

CON = {"max_new_entries_this_cycle": 2, "max_positions": 8, "max_weight_pct_per_sector": 45.0}


def _e(code, rank, w=10.0, action="BUY"):
    return {"code": code, "action": action, "rank": rank, "weight_pct": w}


def test_순위대로_담고_나머지는_미룬다():
    sel = selection.select([_e("C", 3), _e("A", 1), _e("B", 2)], constraints=CON, universe={})
    assert [d["code"] for d in sel.taken] == ["A", "B"]
    assert [d["code"] for d, _ in sel.deferred] == ["C"]
    assert "한도 2건" in sel.deferred[0][1]


def test_걸린_데서_멈추지_않는다():
    """3위가 섹터에 걸려도 4위는 다른 섹터일 수 있다. 멈추면 자리가 빈 채 끝난다."""
    uni = {"A": {"sector": "반도체"}, "B": {"sector": "반도체"}, "C": {"sector": "금융"}}
    con = {**CON, "max_new_entries_this_cycle": 2, "max_weight_pct_per_sector": 15.0}
    sel = selection.select([_e("A", 1), _e("B", 2), _e("C", 3)], constraints=con, universe=uni)
    assert [d["code"] for d in sel.taken] == ["A", "C"]
    assert [d["code"] for d, _ in sel.deferred] == ["B"]


def test_순위없는_후보는_맨_뒤다():
    """없는 것을 1위로 올리면 **순위를 안 매기는 쪽이 유리해진다.**"""
    sel = selection.select(
        [_e("X", None), _e("A", 1)], constraints={"max_new_entries_this_cycle": 1}, universe={}
    )
    assert [d["code"] for d in sel.taken] == ["A"]


def test_순위_결측과_중복을_잡는다():
    assert selection.order_problems([_e("A", None)])
    assert any("겹친다" in x for x in selection.order_problems([_e("A", 1), _e("B", 1)]))
    assert selection.order_problems([_e("A", 1), _e("B", 2)]) == []


def test_ADD_는_보유_종목수를_늘리지_않는다():
    con = {"max_new_entries_this_cycle": 5, "max_positions": 1}
    held = {"A": {"code": "A", "weight_pct": 10.0}}
    sel = selection.select(
        [_e("A", 1, action="ADD"), _e("B", 2)], constraints=con, held=held, universe={}
    )
    assert [d["code"] for d in sel.taken] == ["A"]
    assert "보유 종목 상한" in sel.deferred[0][1]


def test_청산할_종목의_자리는_비운다():
    con = {"max_new_entries_this_cycle": 5, "max_positions": 1}
    held = {"A": {"code": "A", "weight_pct": 10.0}}
    sel = selection.select([_e("B", 1)], constraints=con, held=held, exits={"A"}, universe={})
    assert [d["code"] for d in sel.taken] == ["B"]
