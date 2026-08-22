"""컨텍스트 팩 빌더 테스트.

고정 시드 DB로 결정론적으로 검증한다. 같은 입력 → 같은 팩이어야 한다.
유니버스 선별이 흔들리면 그 자체가 버그다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from data import config as dcfg
from data import store
from decision import config, pack, positions, universe

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
            "adv20_bil_krw": adv,
            "volume_ratio": 1.2,
            "market_cap_bil_krw": cap,
        },
        "flows": {
            "foreign_net_days": f_days,
            "foreign_net_5d_bil_krw": net5,
            "inst_net_days": i_days,
            "inst_net_5d_bil_krw": 0.0,
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


def test_하드필터_악재공시_제외(db):
    db.execute(
        "INSERT INTO disclosures (rcept_no,rcept_dt,corp_code,code,report_nm,category,material,url)"
        " VALUES ('1','20260820','c','000660','상장폐지결정','상장폐지',1,'u')"
    )
    assert "000660" not in universe.hard_filter(db, AS_OF)


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
        "UPDATE indicators SET payload = replace(payload, '\"adv20_bil_krw\": 200.0', "
        "'\"adv20_bil_krw\": 1.0')"
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
    assert dcfg.INGEST_MIN_MARKET_CAP_BIL_KRW == config.MIN_MARKET_CAP_BIL_KRW
    assert dcfg.INGEST_MIN_MARKET_CAP_KRW == config.MIN_MARKET_CAP_BIL_KRW * 1e8


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
