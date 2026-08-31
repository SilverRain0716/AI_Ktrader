"""증거금 등급 적재 (ADR 0013 원칙 1).

여기서 지키는 것 셋.

1. **모르는 종목을 통과시키지 않는다.** 표가 잘려 있으면 전 종목이 조용히 통과한다 —
   실측에서 유니버스 662 중 479종목이 등급 미상이었다.
2. **구분 원문이 파일명을 이긴다.** 파일이 잘못 분류돼 있어도 원문이 맞다.
3. **통째로 교체한다.** 등급이 내려간 종목이 옛 행으로 남는 것이 가장 위험하다.
"""

from __future__ import annotations

import pytest

from data import store
from data.sources import margin


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        yield conn


def _csv(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(
        ",구분,종목번호,종목명\n"
        + "".join(f"{i},'{g},'{c},{n}\n" for i, (g, c, n) in enumerate(rows, 1)),
        encoding="utf-8",
    )
    return p


# ── 1. 파싱 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "pct", "credit", "halted", "caution"),
    [
        ("신용A/담보A/대주A/증20", 20, "A", False, False),
        ("신용B/담보B/증30", 30, "B", False, False),
        ("증50", 50, None, False, False),
        ("정지/증100", 100, None, True, False),
        ("주의/경예/증100", 100, None, False, True),
    ],
)
def test_구분_원문에서_등급을_뜯는다(raw, pct, credit, halted, caution) -> None:
    got = margin.parse_grade(raw, fallback_pct=999)
    assert (got[0], got[1], got[4], got[5]) == (pct, credit, halted, caution)


def test_원문이_파일명을_이긴다() -> None:
    """파일이 잘못 분류돼 있어도 구분 안의 증NN 이 맞다."""
    assert margin.parse_grade("신용A/담보A/증20", fallback_pct=100)[0] == 20


def test_원문에_증NN_이_없으면_파일명을_쓴다() -> None:
    assert margin.parse_grade("신용A/담보A", fallback_pct=30)[0] == 30


# ── 2. 적재 ─────────────────────────────────────────────


def test_폴더를_읽는다(tmp_path) -> None:
    _csv(tmp_path, "20%.csv", [("신용A/담보A/대주A/증20", "000270", "기아")])
    _csv(tmp_path, "100%.csv", [("정지/증100", "900110", "딥커머스")])
    rows = {r.code: r for r in margin.load_dir(tmp_path)}
    assert rows["000270"].margin_pct == 20
    assert rows["900110"].halted is True


def test_같은_종목이_두_등급에_있으면_거부한다(tmp_path) -> None:
    """서로 다른 시점의 파일이 섞인 것이다. 조용히 하나를 고르면 안 된다."""
    _csv(tmp_path, "20%.csv", [("증20", "000270", "기아")])
    _csv(tmp_path, "30%.csv", [("증30", "000270", "기아")])
    with pytest.raises(margin.MarginLoadError, match="양쪽에"):
        margin.load_dir(tmp_path)


def test_알_수_없는_등급은_거부한다(tmp_path) -> None:
    _csv(tmp_path, "20%.csv", [("증77", "000270", "기아")])
    with pytest.raises(margin.MarginLoadError, match="알 수 없는"):
        margin.load_dir(tmp_path)


def test_빈_폴더는_예외다(tmp_path) -> None:
    with pytest.raises(margin.MarginLoadError):
        margin.load_dir(tmp_path)


def test_스냅샷을_쌓고_최신만_조회한다(db, tmp_path) -> None:
    """등급은 과거를 받아올 수 없다. 덮어쓰면 소급 검증이 영영 불가능해진다.

    처음에 전체를 지우고 새로 넣도록 만들었다가 고쳤다 — 뉴스를 매일 쌓기로 한 것과
    같은 이유다(ADR 0011).
    """
    _csv(tmp_path, "20%.csv", [("증20", "000270", "기아"), ("증20", "005930", "삼성전자")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-08-31")

    (tmp_path / "20%.csv").unlink()
    _csv(tmp_path, "100%.csv", [("증100", "000270", "기아")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-09-01")

    # 최신 스냅샷만 본다
    assert margin.latest_as_of(db) == "2026-09-01"
    assert dict(
        db.execute("SELECT code,margin_pct FROM margin_grades WHERE as_of='2026-09-01'")
    ) == {"000270": 100}
    # 옛 스냅샷은 남아 있다
    assert dict(
        db.execute("SELECT code,margin_pct FROM margin_grades WHERE as_of='2026-08-31'")
    ) == {
        "000270": 20,
        "005930": 20,
    }


def test_미래_등급이_새지_않는다(db, tmp_path) -> None:
    """리플레이에서 as_of 이후 스냅샷을 읽으면 11.9 의 사고가 재현된다."""
    _csv(tmp_path, "20%.csv", [("증20", "000270", "기아")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-08-31")
    (tmp_path / "20%.csv").unlink()
    _csv(tmp_path, "100%.csv", [("증100", "000270", "기아")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-09-01")

    assert "000270" in margin.eligible(db, on="2026-08-31"), "그날은 증20% 였다"
    assert "000270" not in margin.eligible(db, on="2026-09-01")


def test_스냅샷이_없으면_빈_결과다(db) -> None:
    """'모르면 통과'로 두면 전 종목이 검사 없이 들어온다."""
    assert margin.eligible(db) == {}
    assert margin.latest_as_of(db) is None


# ── 3. 선별 — 여기가 핵심이다 ───────────────────────────


def test_등급을_모르는_종목은_통과시키지_않는다(db, tmp_path) -> None:
    """표가 잘려 있을 때 조용히 전 종목이 통과하는 것을 막는다.

    실측(2026-08-31): 내려받은 표가 유니버스 662 중 183종목만 덮었다.
    '모르면 통과'로 두면 479종목이 검사 없이 들어온다.
    """
    _csv(tmp_path, "20%.csv", [("증20", "000270", "기아")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-08-31")
    ok = margin.eligible(db)
    assert "000270" in ok
    assert "005930" not in ok, "표에 없는 종목이 통과했다"


def test_정지_경보_종목은_등급이_낮아도_뺀다(db, tmp_path) -> None:
    _csv(
        tmp_path,
        "20%.csv",
        [("정지/증20", "111111", "정지주"), ("주의/증20", "222222", "주의주")],
    )
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-08-31")
    assert margin.eligible(db) == {}


def test_상한을_넘으면_뺀다(db, tmp_path) -> None:
    """실측 근거: 삼천당제약은 시총 4조인데 증100% 다. 시총으로는 못 거른다."""
    _csv(tmp_path, "50%.csv", [("증50", "000250", "삼천당제약")])
    margin.save(db, margin.load_dir(tmp_path), as_of="2026-08-31")
    assert margin.eligible(db, max_pct=40) == {}
    assert "000250" in margin.eligible(db, max_pct=50)
