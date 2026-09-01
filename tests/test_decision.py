"""컨텍스트 팩 빌더 테스트.

고정 시드 DB로 결정론적으로 검증한다. 같은 입력 → 같은 팩이어야 한다.
유니버스 선별이 흔들리면 그 자체가 버그다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from data import config as dcfg
from data import store
from decision import config, pack, positions, universe

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

AS_OF = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 8, 20, tzinfo=dcfg.KST)


# ── 시드 ────────────────────────────────────────────────


def _seed_ohlcv(conn, code, *, days=200, close=50000, halted_recent=False):
    rows = []
    d = AS_OF - timedelta(days=days)
    for i in range(days):
        d += timedelta(days=1)
        halted = 1 if (halted_recent and i >= days - 3) else 0
        rows.append(
            (
                code,
                d.isoformat(),
                close,
                int(close * 1.01),
                int(close * 0.99),
                close,
                0 if halted else 1_000_000,
                None,
                halted,
                "test",
                1,
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,"
        "foreign_hold_pct,halted,source,adjusted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _seed_stock(
    conn,
    code,
    name,
    *,
    adv=200.0,
    cap=10000.0,
    bars=200,
    rsi=60.0,
    rs=5.0,
    ma_aligned=True,
    f_days=0,
    i_days=0,
    net5=0.0,
    market="KOSPI",
    is_pref=0,
    is_spac=0,
    managed=False,
    sector="반도체",
):
    conn.execute(
        "INSERT OR REPLACE INTO listing (code,name,market,sector,sector_group,industry,dept,"
        "is_managed,listing_date,market_cap,shares,is_preferred,is_spac,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            code,
            name,
            market,
            sector,
            sector,
            None,
            None,
            int(managed),
            None,
            cap * 1e8,
            None,
            is_pref,
            is_spac,
            "seed",
        ),
    )
    payload = {
        "indicators": {
            "close": 50000,
            "change_pct": 1.0,
            "ma5": 1,
            "ma20": 1,
            "ma60": 1,
            "ma_aligned": ma_aligned,
            "disparity20_pct": 3.0,
            "rsi14": rsi,
            "macd_hist": 1.0,
            "atr14": 1000.0,
            "atr_pct": 2.0,
            "rs20": rs,
            "high_52w_gap_pct": -5.0,
            "adv20_eok_krw": adv,
            "volume_ratio": 1.2,
            "market_cap_eok_krw": cap,
        },
        "flows": {
            "foreign_net_days": f_days,
            "foreign_net_5d_eok_krw": net5,
            "inst_net_days": i_days,
            "inst_net_5d_eok_krw": 0.0,
            "foreign_hold_pct": 30.0,
            "short_ratio_pct": 1.0,
            "as_of": AS_OF.isoformat(),
        },
        "bars": bars,
    }
    conn.execute(
        "INSERT OR REPLACE INTO indicators (code,date,payload) VALUES (?,?,?)",
        (code, AS_OF.isoformat(), json.dumps(payload, ensure_ascii=False)),
    )
    _seed_ohlcv(conn, code)


def _seed_listing_only(conn, code, name, *, cap=10000.0, managed=False, is_pref=0):
    """모집단에는 들어가지만 일봉·지표가 없는 종목. 적재가 잘렸을 때의 모습이다."""
    conn.execute(
        "INSERT OR REPLACE INTO listing (code,name,market,sector,sector_group,industry,dept,"
        "is_managed,listing_date,market_cap,shares,is_preferred,is_spac,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            code,
            name,
            "KOSPI",
            "기타",
            "기타",
            None,
            None,
            int(managed),
            None,
            cap * 1e8,
            None,
            is_pref,
            0,
            "seed",
        ),
    )


def _seed_index(conn):
    for sym in ("KOSPI", "KOSDAQ"):
        _seed_ohlcv(conn, sym, close=3000)


def _seed_briefing(conn, code, stance="주목", conf="중상", day=None, kind="kr-close-deep"):
    day = day or AS_OF.isoformat()
    bid = f"{day}-1800-{kind}"
    conn.execute(
        """INSERT OR REPLACE INTO briefings (briefing_id,day,stem,kind,published_at,market,
           source_url,summary,heading,sections,disclosure_refs,parse_warnings,view_count,ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bid,
            day,
            f"1800-{kind}",
            kind,
            f"{day}T18:00:00+09:00",
            "KR",
            None,
            "테스트 요약",
            None,
            json.dumps({"마감 지수": "원문" * 500}),
            "[]",
            "[]",
            1,
            "seed",
        ),
    )
    conn.execute(
        """INSERT OR REPLACE INTO briefing_views (briefing_id,seq,day,kind,market,code,symbol,name,
           stance,stance_inherited,confidence,confidence_note,catalyst,reasons,invalidation,
           check_at,kr_links,sources,raw) VALUES (?,0,?,?,'KR',?,NULL,?,?,0,?,NULL,?,?,?,?,'[]','[]',?)""",
        (
            bid,
            day,
            kind,
            code,
            "테스트종목",
            stance,
            conf,
            "촉매",
            json.dumps(["근거1", "근거2"]),
            "틀리는 조건",
            "확인",
            "원문" * 200,
        ),
    )
    return bid


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    store.init_db(conn)
    _seed_index(conn)
    # 하드 필터 통과 + 모멘텀 채널
    _seed_stock(conn, "000660", "SK하이닉스", rs=12.0, rsi=65)
    _seed_stock(conn, "005930", "삼성전자", rs=8.0, rsi=58)
    # 수급 채널
    _seed_stock(conn, "035420", "네이버", ma_aligned=False, f_days=5, net5=300.0, cap=5000.0)
    # 브리핑 채널 (모멘텀·수급 조건 미달)
    _seed_stock(conn, "051910", "LG화학", ma_aligned=False, rsi=40)
    _seed_briefing(conn, "051910")
    # 유니버스 최소 크기(5)를 넘기기 위한 모멘텀 종목 2개
    _seed_stock(conn, "207940", "삼성바이오", rs=6.0, rsi=55, sector="바이오")
    _seed_stock(conn, "005380", "현대차", rs=4.0, rsi=52, sector="자동차")
    # 하드 필터 탈락 3종
    _seed_stock(conn, "900001", "저유동", adv=10.0)
    _seed_stock(conn, "900002", "소형주", cap=500.0)
    _seed_stock(conn, "900003", "우선주", is_pref=1)
    return conn


# ── 하드 필터 ───────────────────────────────────────────


def test_하드필터_유동성_규모_미달_제외(db):
    pool = universe.hard_filter(db, AS_OF)
    assert "000660" in pool
    assert "900001" not in pool, "거래대금 10억은 100억 하한 미달"
    assert "900002" not in pool, "시총 500억은 3,000억 하한 미달"
    assert "900003" not in pool, "우선주는 제외"


def test_하드필터_거래정지_흔적_제외(db):
    _seed_stock(db, "900004", "정지주")
    _seed_ohlcv(db, "900004", halted_recent=True)
    assert "900004" not in universe.hard_filter(db, AS_OF)


def test_하드필터_관리종목_제외(db):
    """FDR의 소속부 표시로 관리종목 118개를 걸러낼 수 있다 (실측)."""
    _seed_stock(db, "900006", "관리종목", managed=True)
    assert "900006" not in universe.hard_filter(db, AS_OF)


def test_하드필터_상장폐지_이력_제외(db):
    db.execute(
        "INSERT INTO delisting (code,delisting_date,name) VALUES ('000660','2026-01-01','x')"
    )
    assert "000660" not in universe.hard_filter(db, AS_OF)


def test_하드필터_유효봉_부족_제외(db):
    _seed_stock(db, "900005", "신규상장", bars=30)
    assert "900005" not in universe.hard_filter(db, AS_OF)


# ── 증거금 등급 필터 (ADR 0013 원칙 1) ──────────────────


def _seed_margin(conn, rows, as_of="2026-08-20"):
    """(code, margin_pct) 목록을 스냅샷으로 심는다."""
    conn.executemany(
        "INSERT OR REPLACE INTO margin_grades "
        "(code,margin_pct,name,grade_raw,halted,caution,as_of) VALUES (?,?,?,?,0,0,?)",
        [(c, p, c, f"증{p}", as_of) for c, p in rows],
    )


def test_증거금_상한_초과_종목은_유니버스에서_빠진다(db):
    """실측 근거: 삼천당제약은 시총 4조인데 증100% 다. 시총 하한으로는 못 거른다."""
    _seed_margin(db, [("000660", 20), ("207940", 100)])
    pool = universe.hard_filter(db, AS_OF)
    assert "000660" in pool
    assert "207940" not in pool, "증100% 종목이 통과했다"


def test_등급을_모르는_종목은_통과하지_못한다(db):
    """표가 일부만 덮고 있을 때 '모르면 통과'로 두면 검사 없이 들어온다.

    실제로 처음 내려받은 표는 유니버스 662 중 183종목만 덮고 있었다.
    """
    _seed_margin(db, [("000660", 20)])
    pool = universe.hard_filter(db, AS_OF)
    assert "000660" in pool
    assert "207940" not in pool, "등급 미상 종목이 통과했다"


def test_스냅샷이_없으면_필터를_끄되_경고를_남긴다(db):
    """조용히 전 종목이 통과하는 것이 이 저장소가 반복해 당한 실패 방식이다.

    끄는 것 자체는 맞다 — 스냅샷 이전 날짜를 리플레이할 수 없으면 백테스트가 막힌다.
    끄고 **말하지 않는 것**이 문제다.
    """
    warns: list[str] = []
    pool = universe.hard_filter(db, AS_OF, warnings=warns)
    assert "000660" in pool, "스냅샷이 없으면 필터는 돌지 않는다"
    assert any("증거금" in w for w in warns), "필터가 꺼진 사실이 어디에도 안 남았다"


def test_미래_등급이_새지_않는다(db):
    """오늘 등급을 과거에 대면 '나중에 강등될 종목'을 미리 피하는 완벽한 미래 정보가 된다.

    거래정지 필터가 상한을 두는 것과 같은 이유다(치명 C).
    """
    _seed_margin(db, [("000660", 20)], as_of="2026-08-01")
    _seed_margin(db, [("000660", 100)], as_of="2026-08-25")  # AS_OF 이후
    assert "000660" in universe.hard_filter(db, AS_OF), "미래 스냅샷이 샜다"


def test_유니버스_결과가_쓴_스냅샷을_밝힌다(db):
    _seed_margin(db, [("000660", 20), ("207940", 20), ("005380", 20)])
    assert universe.build(db, AS_OF).margin_as_of == "2026-08-20"


def test_스냅샷이_없으면_margin_as_of_가_None_이다(db):
    r = universe.build(db, AS_OF)
    assert r.margin_as_of is None
    assert any("증거금" in w for w in r.warnings)


def _seed_disclosure(conn, rcept_no, code, report_nm, day, category=None):
    """실제 적재 경로와 같은 판정을 태운다. 판정을 테스트가 손으로 쓰면
    분류가 틀려도 테스트는 통과한다 — 검증하려던 것이 빠져나간다."""
    from data.sources import dart

    category = category or dart.classify(report_nm)
    conn.execute(
        "INSERT OR REPLACE INTO disclosures "
        "(rcept_no,rcept_dt,corp_code,code,report_nm,category,material,"
        " disqualifying,resolving,url) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            rcept_no,
            day,
            "c",
            code,
            report_nm,
            category,
            int(dart.is_material(category)),
            int(dart.is_disqualifying(report_nm, category)),
            int(dart.is_resolving(report_nm, category)),
            "u",
        ),
    )


def test_하드필터_악재공시_제외(db):
    _seed_disclosure(db, "1", "000660", "주권매매거래정지 (상장폐지 사유발생)", "20260820")
    assert "000660" not in universe.hard_filter(db, AS_OF)


def test_해소_공시는_배제하지_않는다(db):
    """`불성실공시법인미지정`은 지정하지 않았다는 뜻이다. 카테고리만 보면 악재로 뒤집힌다.
    배제가 영구이므로 오탐 한 건이 종목을 영원히 배제한다."""
    _seed_disclosure(db, "1", "000660", "불성실공시법인미지정 (지정유예)", "20260820")
    assert "000660" in universe.hard_filter(db, AS_OF)


def test_예고와_기한안내는_아직_확정이_아니다(db):
    for i, nm in enumerate(
        (
            "불성실공시법인지정예고 (공시번복)",
            "기타시장안내 (상장적격성 실질심사 대상결정 기한 안내)",
            "기타시장안내 (정기보고서 미제출 관련 상장폐지 절차 미진행)",
        )
    ):
        db.execute("DELETE FROM disclosures")
        _seed_disclosure(db, str(i), "000660", nm, "20260820")
        assert "000660" in universe.hard_filter(db, AS_OF), nm


def test_나중에_해소되면_배제가_풀린다(db):
    """실측 사례: 덱스터는 상장적격성 실질심사 대상에서 제외되고 거래정지도 풀렸다.
    영구 배제가 '해소 공시를 무시한다'는 뜻이 되면 정책이 아니라 버그다."""
    _seed_disclosure(db, "1", "000660", "기타시장안내 (상장적격성 실질심사 사유 발생)", "20260701")
    assert "000660" not in universe.hard_filter(db, AS_OF)

    _seed_disclosure(
        db, "2", "000660", "기타시장안내 (상장적격성 실질심사 대상 제외 결정)", "20260810"
    )
    assert "000660" in universe.hard_filter(db, AS_OF)


def test_해소_뒤_사유가_다시_생기면_다시_배제된다(db):
    _seed_disclosure(db, "1", "000660", "기타시장안내 (상장적격성 실질심사 사유 발생)", "20260701")
    _seed_disclosure(
        db, "2", "000660", "기타시장안내 (상장적격성 실질심사 대상 제외 결정)", "20260710"
    )
    assert "000660" in universe.hard_filter(db, AS_OF)

    _seed_disclosure(db, "3", "000660", "주권매매거래정지 (상장폐지 사유발생)", "20260815")
    assert "000660" not in universe.hard_filter(db, AS_OF)


def test_배제는_기간_제한이_없다(db):
    """확정 정책: 시간이 지나도 풀리지 않는다. 푸는 것은 해소 공시뿐이다."""
    _seed_disclosure(db, "1", "000660", "불성실공시법인지정 (공시불이행)", "20200102")
    assert "000660" not in universe.hard_filter(db, AS_OF)


def test_공시가_없으면_팩에_경고가_남는다(db):
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p["data_quality"]["disclosures_since"] is None
    assert any("공시 데이터 없음" in w for w in p["data_quality"]["warnings"])


def test_팩에_공시_적재_시작일이_실린다(db):
    """배제가 영구이므로 배제 집합의 크기가 이 날짜에 달려 있다. 밝히지 않으면
    '이 종목은 왜 유니버스에 없나'에 답할 수 없다."""
    _seed_disclosure(db, "1", "051910", "단일판매ㆍ공급계약체결", "20260601")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p["data_quality"]["disclosures_since"] == "20260601"


# ── 3채널 ───────────────────────────────────────────────


def test_세_채널이_각각_다른_종목을_올린다(db):
    r = universe.build(db, AS_OF)
    by = {c.code: c.channels for c in r.candidates}
    assert "momentum" in by["000660"]
    assert "flow" in by["035420"]
    assert "briefing" in by["051910"]


def test_채널_사유가_기록된다(db):
    r = universe.build(db, AS_OF)
    c = next(x for x in r.candidates if x.code == "051910")
    assert any("briefing:" in s for s in c.screen_reasons)


def test_여러_채널에_걸리면_둘_다_기록(db):
    """근거가 겹치는 종목은 채널 정보가 누적되어야 한다."""
    _seed_briefing(db, "000660")
    c = next(x for x in universe.build(db, AS_OF).candidates if x.code == "000660")
    assert set(c.channels) >= {"briefing", "momentum"}
    assert len(c.screen_reasons) >= 2


def test_보유종목은_유니버스에서_제외(db):
    r = universe.build(db, AS_OF, exclude={"000660"})
    assert "000660" not in {c.code for c in r.candidates}


def test_유니버스_상한이_지켜진다(db):
    for i in range(100):
        _seed_stock(db, f"1{i:05d}", f"종목{i}", rs=float(i))
    r = universe.build(db, AS_OF)
    assert len(r.candidates) <= config.UNIVERSE_MAX


def test_하드필터_통과가_적으면_경고(db):
    r = universe.build(db, AS_OF)
    assert any("3채널 랭킹이 사실상 전수 통과" in w for w in r.warnings)


# ── 포지션 ──────────────────────────────────────────────


def test_순수익률은_수수료와_거래세를_뺀다():
    """총수익률과 섞으면 익절 기준이 조용히 어긋난다."""
    gross = (11000 - 10000) / 10000 * 100
    net = positions.net_yield_pct(10000, 11000)
    assert net < gross
    assert abs(net - 9.77) < 0.05


def test_보유일수는_거래일_기준(db):
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK하이닉스",
        qty=10,
        avg_price=50000,
        opened_at="2026-08-14T09:00:00+09:00",
    )
    p = positions.load_open(db, AS_OF, 100_000_000)[0]
    assert p["held_days"] == 6  # 8/15~8/20, 시드는 달력일마다 봉이 있다
    assert p["code"] == "000660"


def test_포지션_청산시_실현손익_기록(db):
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="x",
        qty=10,
        avg_price=50000,
        opened_at="2026-08-14T09:00:00+09:00",
    )
    positions.close_position(
        db, "p1", closed_at="2026-08-20T15:00:00+09:00", exit_price=55000, exit_reason="익절"
    )
    pnl = db.execute(
        "SELECT realized_pnl_krw FROM paper_positions WHERE position_id='p1'"
    ).fetchone()[0]
    assert 0 < pnl < 50000  # 총차익 5만원보다 작아야 한다 (수수료·세금)
    assert positions.load_open(db, AS_OF, 100_000_000) == []


def test_열린_포지션이_아니면_청산_거부(db):
    with pytest.raises(ValueError, match="열린 포지션이 아니다"):
        positions.close_position(db, "없음", closed_at="x", exit_price=1, exit_reason="y")


# ── 팩 조립 ─────────────────────────────────────────────


def test_팩이_스키마를_통과한다(db):
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p["pack_id"] == "20260820-0820-premarket"
    assert p["cycle"] == "premarket"
    assert len(p["universe"]) >= 3
    assert p["constraints"]["max_positions"] == 8


def test_팩에_브리핑_원문이_들어가지_않는다(db):
    """sections·raw 가 들어가면 팩이 수만 토큰으로 부푼다."""
    _seed_briefing(db, "051910")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    blob = json.dumps(p, ensure_ascii=False)
    assert "원문원문" not in blob
    assert all("sections" not in b for b in p["briefings"])


def test_is_fresh_는_전일_18시_브리핑도_잡는다(db):
    """premarket(08:20)에서 새 정보인 kr-close-deep 은 전일 18:00 이다.
    '당일 여부'로 판정하면 영원히 false 가 된다."""
    yesterday = (AS_OF - timedelta(days=1)).isoformat()
    _seed_briefing(db, "051910", day=yesterday, kind="kr-close-deep")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    b = next(x for x in p["briefings"] if x["kind"] == "kr-close-deep")
    assert b["is_fresh"] is True, "전일 18:00 브리핑은 오늘 아침 판단에 새 정보다"

    # 같은 브리핑이라도 midday 사이클에서는 새 정보가 아니다
    p2 = pack.build(db, cycle="midday", generated_at=NOW)
    b2 = next(x for x in p2["briefings"] if x["kind"] == "kr-close-deep")
    assert b2["is_fresh"] is False


def test_미래_브리핑은_포함되지_않는다(db):
    """08:20 시점에 당일 18:00 브리핑이 보이면 미래를 훔쳐보는 것이다."""
    _seed_briefing(db, "051910", day=AS_OF.isoformat(), kind="kr-close-deep")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert all(b["published_at"] <= NOW.isoformat() for b in p["briefings"])


def test_종목마스터가_비면_거부(db):
    db.execute("DELETE FROM listing")
    with pytest.raises(pack.PackRefused, match="종목 마스터"):
        pack.build(db, cycle="premarket", generated_at=NOW)


def test_일봉이_낡으면_거부(db):
    later = datetime(2026, 9, 1, 8, 20, tzinfo=dcfg.KST)
    with pytest.raises(pack.PackRefused, match="낡았다"):
        pack.build(db, cycle="premarket", generated_at=later)


def test_유니버스가_비면_거부(db):
    """지표가 없어도 하드 필터는 통과 종목 0을 조용히 반환한다. 거부는 그 앞에서 난다."""
    db.execute("DELETE FROM indicators")
    with pytest.raises(pack.PackRefused, match="커버리지"):
        pack.build(db, cycle="premarket", generated_at=NOW)


def test_커버리지는_멀쩡한데_필터가_전부_잘라내면_유니버스로_거부(db):
    """데이터는 다 있는데 임계값이 높아 아무도 못 통과하는 경우. 커버리지 문제와 구분한다."""
    db.execute(
        "UPDATE indicators SET payload = replace(payload, '\"adv20_eok_krw\": 200.0', "
        "'\"adv20_eok_krw\": 1.0')"
    )
    with pytest.raises(pack.PackRefused, match="유니버스"):
        pack.build(db, cycle="premarket", generated_at=NOW)


def test_알수없는_사이클은_예외(db):
    with pytest.raises(ValueError, match="알 수 없는 사이클"):
        pack.build(db, cycle="아무거나", generated_at=NOW)


def test_수급데이터_없으면_경고(db):
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert any("수급 데이터 없음" in w for w in p["data_quality"]["warnings"])


def test_결정론_같은_입력이면_같은_팩(db):
    a = pack.build(db, cycle="premarket", generated_at=NOW)
    b = pack.build(db, cycle="premarket", generated_at=NOW)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def test_팩_저장과_재조회(db):
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    pack.save(db, p)
    pack.save(db, p)  # 멱등
    assert db.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0] == 1
    row = db.execute(
        "SELECT payload FROM context_packs WHERE pack_id=?", (p["pack_id"],)
    ).fetchone()
    assert json.loads(row[0])["pack_id"] == p["pack_id"]


def test_event_사이클은_트리거를_담는다(db):
    p = pack.build(
        db,
        cycle="event",
        generated_at=NOW,
        event_trigger={"kind": "invalidation_hit", "code": "000660", "detail": "SOX 하락"},
    )
    assert p["event_trigger"]["kind"] == "invalidation_hit"


def test_토큰_추정(db):
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert 0 < pack.estimate_tokens(p) < config.MAX_PACK_TOKENS


# ── 유니버스 커버리지 (점검 2026-08-22 결함 2·4) ─────────
# 하드 필터 통과 종목 수는 절단된 모수 위에서 세면 멀쩡해 보인다.
# 실제로 후보 637종목 중 314종목만 적재된 상태에서 205를 세고 통과했다.


def test_적재_하한과_하드필터_시총_하한이_일치한다():
    """어긋나면 조용히 구멍이 생긴다. 낮으면 모집단 결손, 높으면 적재 낭비.
    이 등식이 커버리지 지표가 의미를 갖는 유일한 근거다."""
    assert dcfg.INGEST_MIN_MARKET_CAP_EOK_KRW == config.MIN_MARKET_CAP_EOK_KRW
    assert dcfg.INGEST_MIN_MARKET_CAP_KRW == config.MIN_MARKET_CAP_EOK_KRW * 1e8


def test_지표없는_종목도_모집단에는_들어간다(db):
    """이걸 세지 않으면 '적재된 것 중 상위'를 '시장 상위'로 착각한다."""
    exp0, cov0 = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    _seed_listing_only(db, "900100", "미적재대형주")
    exp1, cov1 = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    assert exp1 == exp0 + 1, "모집단에는 잡혀야 한다"
    assert cov1 == cov0, "지표가 없으니 커버리지에는 안 잡혀야 한다"


def test_전_기간_거래정지는_모집단에서_빠진다(db):
    """유효봉 0이면 지표를 만들 방법이 없다. 모집단에 남기면 커버리지가 영원히 100%에 못 미치고,
    그 미달분이 무슨 뜻인지 아무도 기억하지 못하게 된다 — 이 결함의 정체가 그것이었다."""
    exp0, cov0 = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    _seed_listing_only(db, "900120", "전기간정지")
    _seed_ohlcv(db, "900120", halted_recent=True)
    db.execute("UPDATE ohlcv SET halted=1 WHERE code='900120'")
    exp1, cov1 = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    assert exp1 == exp0, "거래정지 종목은 모집단 밖이어야 한다"
    assert cov1 == cov0


def test_모집단에서_관리종목_우선주_소형주_상폐는_빠진다(db):
    """애초에 후보가 될 수 없는 종목을 모수에 넣으면 커버리지가 영원히 100%가 안 된다."""
    exp0, _ = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    _seed_listing_only(db, "900101", "관리종목", managed=True)
    _seed_listing_only(db, "900102", "우선주", is_pref=1)
    _seed_listing_only(db, "900103", "소형주", cap=500.0)
    _seed_listing_only(db, "900104", "상폐예정")
    db.execute(
        "INSERT OR REPLACE INTO delisting (code,name,market,delisting_date,reason) "
        "VALUES (?,?,?,?,?)",
        ("900104", "상폐예정", "KOSPI", AS_OF.isoformat(), "테스트"),
    )
    exp1, _ = store.universe_coverage(db, min_market_cap=dcfg.INGEST_MIN_MARKET_CAP_KRW)
    assert exp1 == exp0, "넷 다 모집단 밖이어야 한다"


def test_팩에_커버리지가_실린다(db):
    """AI가 자기 시야가 얼마나 좁은지 알아야 abstain 판단을 할 수 있다."""
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    cov = p["data_quality"]["universe_coverage"]
    assert cov["expected"] == cov["covered"]
    assert cov["pct"] == 1.0


def test_커버리지가_경고선_아래면_경고를_남긴다(db):
    _seed_listing_only(db, "900110", "미적재1")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    cov = p["data_quality"]["universe_coverage"]
    assert cov["pct"] < config.UNIVERSE_COVERAGE_WARN
    assert cov["pct"] >= config.UNIVERSE_COVERAGE_REFUSE
    assert any("커버리지" in w for w in p["data_quality"]["warnings"])


def test_커버리지가_하한_아래면_팩을_거부한다(db):
    """이 검사가 없어서 49% 상태로 유니버스를 뽑고 있었다."""
    for n in range(4):
        _seed_listing_only(db, f"9002{n:02d}", f"미적재{n}")
    with pytest.raises(pack.PackRefused, match="커버리지"):
        pack.build(db, cycle="premarket", generated_at=NOW)


def test_커버리지_거부는_유니버스_구축보다_먼저_난다(db):
    """모수가 깨졌으면 스크리닝 결과 전체가 무의미하다. 낡은 일봉과 같은 취급이다."""
    for n in range(4):
        _seed_listing_only(db, f"9003{n:02d}", f"미적재{n}")
    calls = []
    orig = universe.build
    universe.build = lambda *a, **k: calls.append(1) or orig(*a, **k)
    try:
        with pytest.raises(pack.PackRefused, match="커버리지"):
            pack.build(db, cycle="premarket", generated_at=NOW)
    finally:
        universe.build = orig
    assert not calls, "커버리지가 깨졌는데 유니버스를 만들었다"


# ── 회계·시점 (점검 2026-08-23 치명 A·B·C·E) ────────────


def test_손실은_매수여력을_늘리지_않는다(db):
    """cash = 시드 − 취득원가 + 실현손익. 평가금을 빼면 손익이 현금으로 둔갑한다 —
    -30% 나면 현금이 30% 늘어 물타기를 구조적으로 유도했다."""
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK",
        qty=1000,
        avg_price=50000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    cash = []
    for px in (50000, 35000, 65000):
        db.execute(
            "UPDATE ohlcv SET close=?, high=?, low=?, open=? WHERE code='000660'", (px, px, px, px)
        )
        cash.append(positions.account_state(db, 100_000_000)["cash_available_krw"])
    assert len(set(cash)) == 1, f"주가에 따라 현금이 변했다: {cash}"


def test_총자산이_평가손익을_따라간다(db):
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK",
        qty=1000,
        avg_price=50000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    db.execute("UPDATE ohlcv SET close=35000 WHERE code='000660'")
    a = positions.account_state(db, 100_000_000)
    assert a["total_equity_krw"] == a["cash_available_krw"] + a["holdings_value_krw"]
    assert a["total_equity_krw"] < 100_000_000, "평가손실이 총자산에 반영되지 않았다"


def test_실현손실이_계좌에서_사라지지_않는다(db):
    """total_equity 를 상수로 두면 손절해도 총자산이 그대로다."""
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK",
        qty=1000,
        avg_price=50000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    positions.close_position(
        db, "p1", closed_at="2026-08-20T15:00:00+09:00", exit_price=35000, exit_reason="손절"
    )
    a = positions.account_state(db, 100_000_000)
    assert a["realized_pnl_total_krw"] < -14_000_000
    assert a["total_equity_krw"] < 86_000_000


def test_당일_손절_종목은_재진입이_금지된다(db):
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK",
        qty=10,
        avg_price=50000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    positions.close_position(
        db,
        "p1",
        closed_at=f"{AS_OF.isoformat()}T15:00:00+09:00",
        exit_price=35000,
        exit_reason="손절",
    )
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert "000660" in p["constraints"]["blocked_codes"]


def test_이익_청산은_재진입을_막지_않는다(db):
    positions.open_position(
        db,
        position_id="p1",
        code="000660",
        name="SK",
        qty=10,
        avg_price=50000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    positions.close_position(
        db,
        "p1",
        closed_at=f"{AS_OF.isoformat()}T15:00:00+09:00",
        exit_price=70000,
        exit_reason="익절",
    )
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p["constraints"]["blocked_codes"] == []


def test_브리핑_채널이_미래_브리핑을_읽지_않는다(db):
    """pack 의 브리핑 블록은 상한을 걸고 있었는데 유니버스 채널 쿼리에는 없었다.
    팩에는 없는 브리핑을 근거로 종목이 유니버스에 오르는 모순이 생겼다."""
    from datetime import time as dtime

    _seed_briefing(db, "051910", day=AS_OF.isoformat(), kind="kr-close-deep")
    db.execute(
        "UPDATE briefings SET published_at=? WHERE briefing_id=?",
        (f"{AS_OF.isoformat()}T18:00:00+09:00", f"{AS_OF.isoformat()}-1800-kr-close-deep"),
    )

    morning = datetime.combine(AS_OF, dtime(8, 20), tzinfo=dcfg.KST)
    res = universe.build(db, AS_OF, now=morning)
    reasons = [r for c in res.candidates for r in c.screen_reasons if r.startswith("briefing")]
    assert reasons == [], f"08:20 에 18:00 브리핑을 읽었다: {reasons}"

    evening = datetime.combine(AS_OF, dtime(18, 30), tzinfo=dcfg.KST)
    res2 = universe.build(db, AS_OF, now=evening)
    reasons2 = [r for c in res2.candidates for r in c.screen_reasons if r.startswith("briefing")]
    assert reasons2, "발행 후에는 읽어야 한다"


def test_미래_거래정지를_미리_피하지_않는다(db):
    """as_of 이후의 정지 이력이 오늘의 하드 필터에 영향을 주면 완벽한 미래 정보다."""
    before = set(universe.hard_filter(db, AS_OF))
    future = (AS_OF + timedelta(days=10)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO ohlcv (code,date,open,high,low,close,volume,"
        "foreign_hold_pct,halted,source,adjusted) "
        "VALUES ('000660',?,1,1,1,1,0,NULL,1,'t',1)",
        (future,),
    )
    assert set(universe.hard_filter(db, AS_OF)) == before


def test_월요일_아침에_거부되지_않는다(db):
    """달력일로 세면 금요일 배치 → 월요일 아침이 3일 낡음으로 잡혔다."""
    db.execute("DELETE FROM ohlcv WHERE code IN ('KOSPI','KOSDAQ')")
    for d in ("2026-08-19", "2026-08-20", "2026-08-21"):  # 수·목·금
        for sym in ("KOSPI", "KOSDAQ"):
            db.execute(
                "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted)"
                " VALUES (?,?,3000,3000,3000,3000,1,0,'t',1)",
                (sym, d),
            )
    pack.refuse_if_stale(db, date(2026, 8, 24))  # 월요일 — 거부되면 예외


def test_실제로_낡으면_여전히_거부한다(db):
    db.execute("DELETE FROM ohlcv WHERE code IN ('KOSPI','KOSDAQ')")
    for d in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"):
        for sym in ("KOSPI", "KOSDAQ"):
            db.execute(
                "INSERT INTO ohlcv (code,date,open,high,low,close,volume,halted,source,adjusted)"
                " VALUES (?,?,3000,3000,3000,3000,1,0,'t',1)",
                (sym, d),
            )
    with pytest.raises(pack.PackRefused, match="낡았다"):
        pack.refuse_if_stale(db, date(2026, 8, 27))  # 목요일 — 거래일 4회분


def test_관리종목_판정_불가는_경고로_드러난다(db):
    """is_managed=0 이 '아니다'와 '모른다'를 겸하면 판정 실패가 조용히 통과가 된다."""
    db.execute("UPDATE listing SET is_managed_known=0")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert any("판정 불가" in w for w in p["data_quality"]["warnings"])

    db.execute("UPDATE listing SET is_managed_known=1")
    p2 = pack.build(db, cycle="premarket", generated_at=NOW)
    assert not any("판정 불가" in w for w in p2["data_quality"]["warnings"])


# ── 리스크 한도 환경변수 ────────────────────────────────
# 예전에는 기본값이 코드에 박혀 있었고, .env.example 에는 AIK_* 가 하나도 없었다.
# 그래서 아무도 설정하지 않은 채 초안 값으로 돌고 있었고, 그 사실이 드러나지 않았다.
# 아래 네 가지가 각각 그 상태로 되돌아가는 경로를 막는다.


def _read_all_limits() -> None:
    """한도 8개를 전부 읽는다. 어느 항목이 빠져도 여기서 걸린다."""
    config.constraints()
    config.account_seed()


def test_스키마와_코드의_경계가_같다():
    """범위가 두 곳에 있다. 한쪽만 고치면 계약과 구현이 조용히 갈라진다."""
    schema = json.loads((SCHEMA_DIR / "context_pack.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]["constraints"]["properties"]
    for env_name, (_, lo, hi, _label) in config._LIMIT_SPECS.items():
        if env_name == "AIK_PAPER_EQUITY_KRW":
            continue  # 시드는 account 블록이고 스키마에 경계를 두지 않는다
        key = env_name.removeprefix("AIK_").lower()
        key = {"max_new_entries_per_cycle": "max_new_entries_this_cycle"}.get(key, key)
        assert props[key]["minimum"] == lo, key
        assert props[key]["maximum"] == hi, key
    # 7개 전부가 필수여야 한다 — 하나라도 빠지면 그 항목을 지워도 팩이 통과한다.
    assert set(schema["properties"]["constraints"]["required"]) == set(props) - {
        "daily_loss_limit_hit",
        "blocked_codes",
    }


@pytest.mark.parametrize("name", list(config._LIMIT_SPECS))
def test_한도가_없으면_예외다(monkeypatch, name):
    """설정 누락은 조용한 폴백이 아니라 정지다. 8개 전부를 각각 확인한다."""
    monkeypatch.delenv(name)
    with pytest.raises(config.RiskLimitError) as e:
        _read_all_limits()
    assert name in str(e.value)
    # 사람이 읽고 바로 고칠 수 있어야 한다.
    assert ".env" in str(e.value)


@pytest.mark.parametrize("name", list(config._LIMIT_SPECS))
def test_빈_문자열도_미설정으로_본다(monkeypatch, name):
    """.env.example 은 8개를 전부 `AIK_X=` 로 내보낸다 — 복사만 하고 안 채운 상태가
    가장 흔한 실패다. 이건 '설정됨'이 아니라 '미설정'이어야 한다."""
    monkeypatch.setenv(name, "   ")
    with pytest.raises(config.RiskLimitError) as e:
        _read_all_limits()
    # '읽을 수 없다'(파싱 실패)가 아니라 '설정되지 않았다'로 걸려야 한다.
    # 이 단정이 없으면 공백 처리를 지워도 테스트가 통과한다.
    assert "설정되지 않았다" in str(e.value)
    assert name in str(e.value)


def test_숫자가_아니면_예외다(monkeypatch):
    """`여덟` 을 넣으면 예전에는 조용히 8 로 돌아갔다. 설정한 줄 알았는데 아니었다."""
    monkeypatch.setenv("AIK_MAX_POSITIONS", "여덟")
    with pytest.raises(config.RiskLimitError) as e:
        config.constraints()
    assert "여덟" in str(e.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIK_MAX_WEIGHT_PCT_PER_NAME", "1200"),  # 12.00 의 소수점 실수
        ("AIK_MAX_POSITIONS", "0"),  # 한 종목도 못 사는 설정
        ("AIK_MAX_POSITIONS", "500"),  # 사실상 한도 없음
        ("AIK_MAX_RISK_PCT_PER_TRADE", "50"),  # 1회에 계좌의 절반
        ("AIK_DAILY_LOSS_LIMIT_KRW", "-1000"),  # 부호 실수
    ],
)
def test_범위를_벗어난_값은_예외다(monkeypatch, name, value):
    """숫자로 읽히기만 하면 통과하던 구간. 단위·소수점·부호 실수를 여기서 끊는다."""
    monkeypatch.setenv(name, value)
    with pytest.raises(config.RiskLimitError) as e:
        config.constraints()
    msg = str(e.value)
    assert name in msg
    # '미설정' 메시지에도 변수명이 들어 있다. 범위 위반으로 걸린 것임을 구분해야
    # 범위 검사를 지워도 통과하는 테스트가 되지 않는다.
    assert "허용 범위를 벗어났다" in msg


def test_페이퍼_시드도_필수다(monkeypatch):
    """가상 자금이어도 자금 규모다 (ADR 0004). 1억이 코드에 박혀 있었다."""
    monkeypatch.delenv("AIK_PAPER_EQUITY_KRW")
    with pytest.raises(config.RiskLimitError):
        config.account_seed()


def test_missing_limits_는_빠진_항목을_전부_알려준다(monkeypatch):
    """하나씩 고쳐가며 재실행하지 않아도 되게, 진단은 한 번에 다 준다."""
    monkeypatch.delenv("AIK_MAX_POSITIONS")
    monkeypatch.setenv("AIK_MAX_RISK_PCT_PER_TRADE", "99")
    missing = config.missing_limits()
    assert "AIK_MAX_POSITIONS" in missing
    assert "AIK_MAX_RISK_PCT_PER_TRADE" in missing
    assert "AIK_MAX_WEIGHT_PCT_PER_NAME" not in missing


def test_한도_없이는_팩을_만들지_않는다(db, monkeypatch):
    """한도 없이 만든 팩은 AI 에게 '제한이 없다'고 말하는 것과 같다."""
    monkeypatch.delenv("AIK_MAX_POSITIONS")
    with pytest.raises(config.RiskLimitError):
        pack.build(db, cycle="premarket", generated_at=NOW)


def test_손실한도_0은_경고로_드러난다(db, monkeypatch):
    """0 은 명시적 선택이지만, '한도를 껐다'는 사실이 팩 안에서 보여야 한다."""
    monkeypatch.setenv("AIK_DAILY_LOSS_LIMIT_KRW", "0")
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert any("일일 손실 한도가 0" in w for w in p["data_quality"]["warnings"])

    monkeypatch.setenv("AIK_DAILY_LOSS_LIMIT_KRW", "1000000")
    p2 = pack.build(db, cycle="premarket", generated_at=NOW)
    assert not any("일일 손실 한도가 0" in w for w in p2["data_quality"]["warnings"])
    assert p2["constraints"]["daily_loss_limit_krw"] == 1_000_000


@pytest.mark.parametrize(
    "raw",
    [
        "８",  # 전각 숫자 — int() 는 조용히 받는다
        "٨",  # 아랍-인도 숫자 — 마찬가지
        "1_0",  # 자릿수 구분 언더바. 손으로 고치는 .env 에서 10 이 되면 사고다
        "8.0",  # 정수 칸에 소수점
        "1e9",  # 지수 표기
    ],
)
def test_사람이_쓴_숫자처럼_보이지만_아닌_값은_거부한다(monkeypatch, raw):
    """파이썬의 int()/float() 는 이것들을 전부 조용히 받아들인다.

    설정 파일에서 `1_0` 이 10 으로 읽히는 것은 오타가 통과하는 것이지 관대함이 아니다.
    """
    monkeypatch.setenv("AIK_MAX_POSITIONS", raw)
    with pytest.raises(config.RiskLimitError) as e:
        config.constraints()
    assert "읽을 수 없다" in str(e.value)


def test_항목은_멀쩡한데_조합이_모순이면_거부한다(monkeypatch):
    """범위 검사는 한 칸씩만 본다. 두 칸의 어긋남이 실제로는 더 흔하다."""
    # 종목 비중 상한 > 섹터 비중 상한 — 한 종목이 자기 섹터에조차 못 들어간다
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_NAME", "40.0")
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_SECTOR", "30.0")
    with pytest.raises(config.RiskLimitError, match="자기 섹터"):
        config.constraints()

    # 들어갈 자리보다 많이 사려는 설정
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_NAME", "12.0")
    monkeypatch.setenv("AIK_MAX_POSITIONS", "2")
    monkeypatch.setenv("AIK_MAX_NEW_ENTRIES_PER_CYCLE", "5")
    with pytest.raises(config.RiskLimitError, match="들어갈 자리"):
        config.constraints()


def test_조합_모순도_missing_limits_가_알려준다(monkeypatch):
    """개별 항목이 다 멀쩡하면 진단이 빈 목록을 주던 구간."""
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_NAME", "40.0")
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_SECTOR", "30.0")
    bad = config.missing_limits()
    assert bad and "조합 모순" in bad[0]


def test_자릿수가_다른_한도_조합은_경고로_드러난다(db, monkeypatch):
    """오류는 아니다 — 현금을 남기려는 의도일 수 있다. 다만 자릿수가 틀린 건 보여야 한다."""
    monkeypatch.setenv("AIK_MAX_POSITIONS", "20")
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_NAME", "0.5")  # 20 × 0.5 = 10%
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert any("절반도 쓸 수 없는" in w for w in p["data_quality"]["warnings"])

    # 8 × 12 = 96%. 현금 4%를 남기는 평범한 설정이다 — 경고가 뜨면 안 된다.
    # 정상 설정에서 상시로 켜지는 경고는 data_quality 를 오염시킨다.
    monkeypatch.setenv("AIK_MAX_POSITIONS", "8")
    monkeypatch.setenv("AIK_MAX_WEIGHT_PCT_PER_NAME", "12.0")
    p2 = pack.build(db, cycle="premarket", generated_at=NOW)
    assert not any("쓸 수 없는" in w for w in p2["data_quality"]["warnings"])


def test_손실한도는_실제로_걸린다(db, monkeypatch):
    """`daily_loss_limit_hit` 은 한때 하드코딩 상수였다. 지금은 실제 실현손익을 본다.

    conftest 가 한도를 0(비활성)으로 고정하고 있어 이 경로는 테스트에 잡히지 않고 있었다.
    """
    monkeypatch.setenv("AIK_DAILY_LOSS_LIMIT_KRW", "1000000")
    today = NOW.date().isoformat()
    db.execute(
        "INSERT INTO paper_positions (code,name,qty,avg_price,opened_at,closed_at,"
        "exit_price,exit_reason,realized_pnl_krw) "
        "VALUES ('005930','삼성전자',10,70000,?,?,60000,'STOP',-1500000)",
        (today, today),
    )
    p = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p["constraints"]["daily_loss_limit_hit"] is True

    # 한도를 손실보다 크게 잡으면 걸리지 않는다 — 상수가 아니라 비교의 결과다.
    monkeypatch.setenv("AIK_DAILY_LOSS_LIMIT_KRW", "9000000")
    p2 = pack.build(db, cycle="premarket", generated_at=NOW)
    assert p2["constraints"]["daily_loss_limit_hit"] is False


def test_수급_신선도는_거래일_기준이다(tmp_path) -> None:
    """달력일로 세면 매주 월요일마다 경고가 떴다 — 금요일이 마지막 거래일이라 3일 낡음이 된다.

    일봉이 이미 겪고 고친 문제(11.9)가 수급에만 남아 있었다. 같은 함수를 쓰게 했다.
    """
    from datetime import date

    from data import store
    from decision import config, pack

    assert not hasattr(config, "MAX_FLOWS_STALE_DAYS"), "달력일 상수가 남아 있다"

    with store.connect(tmp_path / "t.db") as conn:
        store.init_db(conn)
        for d in ("2026-08-26", "2026-08-27", "2026-08-28"):
            conn.execute(
                "INSERT INTO ohlcv "
                "(code,date,open,high,low,close,volume,halted,source,adjusted) "
                "VALUES ('KOSPI',?,1,1,1,1,1,0,'test',1)",
                (d,),
            )
        # 금(8/28) 마감 뒤 월(8/31) 아침. 달력일이면 3, 거래일 기준이면 1 이하다.
        missed = pack._sessions_missed(conn, "2026-08-28", date(2026, 8, 31))

    assert missed <= config.MAX_FLOWS_STALE_SESSIONS, (
        f"금→월이 {missed} 회로 나와 경고가 뜬다 (달력일이면 3)"
    )


def test_지수가_낡으면_보수적으로_판정한다(tmp_path) -> None:
    """지수 봉 0개는 "장이 안 섰다"와 "지수도 같이 낡았다"를 구분하지 못한다.

    구분이 안 될 때는 낡은 쪽으로 본다 — `if row[0]:` 의 달력 폴백이 그 장치다.
    한 번 이것을 버그로 보고 0 을 그대로 돌려주게 고쳤다가
    `test_실제로_낡으면_여전히_거부한다` 가 잡았다.
    """
    from datetime import date

    from data import store
    from decision import pack

    with store.connect(tmp_path / "a.db") as conn:
        store.init_db(conn)
        conn.execute(
            "INSERT INTO ohlcv "
            "(code,date,open,high,low,close,volume,halted,source,adjusted) "
            "VALUES ('KOSPI','2026-08-21',1,1,1,1,1,0,'test',1)"
        )
        # 지수가 8/21 에 멈춰 있고 오늘은 8/27 — 그 사이 장은 실제로 섰다.
        # 지수 봉이 없다고 "0 회 지남"으로 읽으면 낡은 데이터가 최신으로 통과한다.
        assert pack._sessions_missed(conn, "2026-08-21", date(2026, 8, 27)) > 0


# ── arm 별 독립 가상 계좌 (3-arm 대응비교) ──────────────


def test_arm_마다_계좌가_독립이다(db):
    """**계좌가 하나면 3-arm 대응비교가 불가능하다**(2026-09-01 발견).

    `cash = 시드 − Σ취득원가 + Σ실현손익` 이 전체 합산이라 Arm 1 의 매수가 Arm 2 의
    현금·비중·섹터 한도를 깎았다. ADR 0005 는 차이를 재는 법만 정하고 계좌 분리를 적지 않았다.
    """
    positions.open_position(
        db,
        position_id="a1",
        arm=1,
        code="000660",
        name="SK하이닉스",
        qty=10,
        avg_price=50_000,
        opened_at="2026-08-20T09:00:00+09:00",
    )
    seed = 100_000_000
    a1 = positions.account_state(db, seed, arm=1)
    a2 = positions.account_state(db, seed, arm=2)

    assert a1["cash_available_krw"] < seed, "arm 1 은 매수했다"
    assert a2["cash_available_krw"] == seed, "arm 2 는 아무것도 안 샀는데 현금이 줄었다"
    assert a1["holdings_value_krw"] > 0 and a2["holdings_value_krw"] == 0


def test_arm_마다_보유_목록이_다르다(db):
    for arm, code in ((1, "000660"), (2, "005930")):
        positions.open_position(
            db,
            position_id=f"p{arm}",
            arm=arm,
            code=code,
            name=code,
            qty=1,
            avg_price=50_000,
            opened_at="2026-08-20T09:00:00+09:00",
        )
    assert [p["code"] for p in positions.load_open(db, AS_OF, 10**8, arm=1)] == ["000660"]
    assert [p["code"] for p in positions.load_open(db, AS_OF, 10**8, arm=2)] == ["005930"]


def test_당일_손절_종목_금지도_arm_별이다(db):
    """arm 1 이 손절한 종목을 arm 2 가 못 사면 그것도 간섭이다."""
    positions.open_position(
        db,
        position_id="p1",
        arm=1,
        code="000660",
        name="SK하이닉스",
        qty=1,
        avg_price=50_000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    positions.close_position(
        db, "p1", closed_at="2026-08-20T14:00:00+09:00", exit_price=40_000, exit_reason="stop"
    )
    day = date(2026, 8, 20)
    assert positions.blocked_codes_on(db, day, arm=1) == ["000660"]
    assert positions.blocked_codes_on(db, day, arm=2) == []


def test_실현손익도_arm_별이다(db):
    positions.open_position(
        db,
        position_id="p1",
        arm=1,
        code="000660",
        name="x",
        qty=1,
        avg_price=50_000,
        opened_at="2026-08-19T09:00:00+09:00",
    )
    positions.close_position(
        db, "p1", closed_at="2026-08-20T14:00:00+09:00", exit_price=40_000, exit_reason="stop"
    )
    assert positions.realized_pnl_total(db, arm=1) < 0
    assert positions.realized_pnl_total(db, arm=2) == 0
