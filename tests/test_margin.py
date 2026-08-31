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


# ── 5. API 경로 (정본) ──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [("30%", 30), ("100%", 100), ("30", 30), (" 40 %", 40), ("20%", 20)],
)
def test_증거금률_문자열을_읽는다(raw, want) -> None:
    assert margin.parse_rate(raw) == want


@pytest.mark.parametrize("raw", ["", None, "알수없음", "15%", "0%", "999%"])
def test_모르는_형식은_None_이지_0_이_아니다(raw) -> None:
    """0 으로 접으면 '증거금 0%' 라는 가장 우량한 등급으로 통과한다 — 정확히 반대다."""
    assert margin.parse_rate(raw) is None


class _FakeKiwoom:
    """`kt00011` 응답만 흉내낸다."""

    def __init__(self, table, boom=()):
        self.table, self.boom, self.calls = table, set(boom), []

    def post(self, tr, path, body):
        code = body["stk_cd"]
        self.calls.append((tr, code))
        if code in self.boom:
            raise RuntimeError("유량 한도 초과")
        return {"return_code": 0, "stk_profa_rt": self.table.get(code)}


def test_API_로_등급을_받는다() -> None:
    """실측(2026-09-01): CSV·모의 API·실전 API 가 10종목 전부 일치했다."""
    c = _FakeKiwoom({"000880": "30%", "000250": "100%"})
    rows, failed = margin.fetch_api([("000880", 135500), ("000250", 200000)], client=c)
    assert {r.code: r.margin_pct for r in rows} == {"000880": 30, "000250": 100}
    assert not failed
    assert c.calls[0][0] == margin.MARGIN_TR


def test_읽을_수_없는_응답은_실패로_남는다() -> None:
    """일부만 받고 전체인 척하면 유니버스에 구멍이 뚫린 채로 필터가 돈다."""
    c = _FakeKiwoom({"000880": "30%", "999999": "알수없음"})
    rows, failed = margin.fetch_api([("000880", 1), ("999999", 1)], client=c)
    assert [r.code for r in rows] == ["000880"]
    assert "999999" in failed


def test_개별_실패가_전체를_멈추지_않는다() -> None:
    c = _FakeKiwoom({"000880": "30%", "000250": "100%"}, boom={"000880"})
    rows, failed = margin.fetch_api([("000880", 1), ("000250", 1)], client=c)
    assert [r.code for r in rows] == ["000250"]
    assert "유량" in failed["000880"]


def test_API_는_정지_경보를_모른다고_말한다() -> None:
    """`kt00011` 은 시장경보를 주지 않는다. **모르는 것을 '아니오'로 채우면 안 되므로**
    그 판정은 다른 소스(listing.is_managed·거래정지 봉)가 맡는다.
    """
    rows, _ = margin.fetch_api([("000880", 1)], client=_FakeKiwoom({"000880": "30%"}))
    assert rows[0].halted is False and rows[0].caution is False
    assert "api" in rows[0].grade_raw


def test_daily_배치가_증거금을_받는다() -> None:
    """CSV 수동 적재만 두면 등급이 바뀌어도 아무 신호가 없다."""
    import inspect

    from data import pipeline as dp

    src = inspect.getsource(dp.main)
    daily = src[src.index('== "daily"') :]
    daily = daily[: daily.index("task_status")]
    assert "task_margin" in daily, "daily 배치에서 증거금 조회가 빠졌다"


def test_지수_코드가_증거금_조회에_섞이지_않는다() -> None:
    """**'KOSDAQ' 은 정확히 6자다** — 길이로는 안 걸러진다.

    실제로 그 때문에 kt00011 이 "종목정보가 존재하지 않습니다"로 실패했다(2026-09-01).
    """
    import inspect

    from data import pipeline as dp

    src = inspect.getsource(dp.task_margin)
    sql = "".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    assert "listing" in sql, "지수를 거르려면 상장 목록과 조인해야 한다"


def test_숫자_6자리로_좁히면_실제_종목이_잘린다() -> None:
    """숫자만 남기는 필터는 **보통주를 버린다.**

    실측(2026-09-01): 삼성에피스홀딩스(0126Z0)·에임드바이오(0009K0)·
    한화머시너리앤서비스홀딩스(0220W0)·삼양바이오팜(0120G0) 넷이 전부 보통주인데
    코드에 문자가 있다. 그래서 GLOB 이 아니라 상장 목록 조인이 답이다.
    """
    import inspect
    import re

    from data import pipeline as dp

    src = inspect.getsource(dp.task_margin)
    sql = "".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    assert not re.search(r"GLOB\s*'\[0-9\]", sql), "숫자 6자리 필터는 실제 종목을 자른다"
