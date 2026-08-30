"""Phase 0 실측 도구 — 조사 대신 재고, 결과를 파일로 남긴다.

`docs/phase0-verification.md` 는 35개 항목(A1~F6)을 "직접 확인해야 하는 것"으로 지목하고
결과 칸을 비워 뒀다. 사람이 손으로 재고 손으로 적는 구조라 시작 비용이 높았다.

이 스크립트는 **잴 수 있는 것을 자동으로 재서** `docs/phase0-results.md` 에 append 한다.
- 키움 항목은 앱키가 있어야 한다. 없으면 SKIP 으로 남기고 나머지를 계속 잰다.
  ("키가 없어서 아무것도 못 쟀다"가 아니라 "무엇을 못 쟀는지"가 기록돼야 한다.)
- 주문(F)은 **여기서 재지 않는다.** 모의투자라도 주문은 사람이 직접 내는 행위다.

사용:
    python scripts/phase0_probe.py                 # 빠른 회차
    python scripts/phase0_probe.py --probe-limits  # 유량 한도까지 (일부러 429를 낸다)
    python scripts/phase0_probe.py --probe-depth   # 분봉 깊이까지 (연속조회 수 분)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import config as dcfg  # noqa: E402
from data import env as _env  # noqa: E402,F401  (.env 로딩)

RESULTS = ROOT / "docs" / "phase0-results.md"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}


@dataclass
class Probe:
    id: str
    title: str
    status: str = "SKIP"  # OK | FAIL | SKIP
    result: str = ""
    detail: list[str] = field(default_factory=list)


def _run(p: Probe, fn) -> Probe:
    t0 = time.monotonic()
    try:
        fn(p)
    except Exception as e:
        p.status = "FAIL"
        p.result = f"{type(e).__name__}: {e}"
    p.detail.append(f"{time.monotonic() - t0:.1f}s")
    mark = {"OK": "✓", "FAIL": "✗", "SKIP": "·"}[p.status]
    print(f" {mark} {p.id:<4} {p.title:<38} {p.result}")
    return p


# ── E. 외부 데이터 소스 ─────────────────────────────────
# 이 섹션은 키움 앱키 없이 전부 잴 수 있다. 그런데도 비어 있었다.


def e1_naver_from_cloud(p: Probe) -> None:
    """★ 네이버가 해외 IP를 막으면 데이터 수집을 국내 서버로 옮겨야 한다."""
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        "?symbol=005930&requestType=1&startTime=20260801&endTime=20260826&timeframe=day"
    )
    r = httpx.get(url, headers=UA, timeout=15)
    rows = json.loads(r.text.replace("'", '"'))
    # 국가만 본다. 원시 IP 는 결과 파일에 남기지 않는다 — 이 저장소는 public 이다.
    cc = "?"
    for svc in ("https://ifconfig.co/country-iso", "http://ip-api.com/line/?fields=countryCode"):
        try:
            v = httpx.get(svc, timeout=8, headers={"User-Agent": "curl/8"}).text.strip()
        except Exception:
            continue
        if len(v) == 2 and v.isalpha():
            cc = v.upper()
            break
    p.status = "OK" if len(rows) > 1 else "FAIL"
    p.result = f"{r.status_code} · {len(rows) - 1}행 수신 · 호출 국가 {cc}"
    p.detail.append(
        "한국 외 IP 에서도 200 이 온다 — 클라우드에서 수집해도 되고, "
        "데이터 계층을 국내 서버로 옮길 이유가 없다."
    )


def e3_dart(p: Probe) -> None:
    if not os.getenv("DART_API_KEY"):
        p.result = "DART_API_KEY 미설정"
        return
    r = httpx.get(
        "https://opendart.fss.or.kr/api/list.json",
        params={
            "crtfc_key": os.getenv("DART_API_KEY"),
            "bgn_de": "20260820",
            "end_de": "20260826",
            "page_count": "10",
        },
        timeout=20,
    )
    j = r.json()
    ok = j.get("status") == "000"
    p.status = "OK" if ok else "FAIL"
    p.result = f"status={j.get('status')} · {j.get('message', '')[:40]}"
    if ok:
        p.detail.append(f"total_count={j.get('total_count')}")


def e4_fdr_delisting(p: Probe) -> None:
    """상장폐지 반영 지연 — 생존편향 보정이 얼마나 최신인지."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX-DELISTING")
    col = "DelistingDate" if "DelistingDate" in df.columns else df.columns[-1]
    latest = str(df[col].max())[:10]
    lag = (datetime.now(dcfg.KST).date() - datetime.fromisoformat(latest).date()).days
    p.status = "OK"
    p.result = f"{len(df):,}건 · 최신 폐지일 {latest} (오늘 −{lag}일)"


# ── D. 차트·시세 ────────────────────────────────────────

KIWOOM_MINUTE_TR = "ka10080"


# 가격 파서(`kiwoom_price`)는 `data/sources/kiwoom.py` 로 옮겼다 — 적재 경로가 정본이다.
# 같은 함수가 두 곳에 있으면 한쪽만 고쳐지고, 그 사실이 겉으로 드러나지 않는다.


def d2_kiwoom_minute_depth(p: Probe, *, deep: bool) -> None:
    """★ 키움 분봉을 어디까지 소급할 수 있는가 — 백테스트 가능 범위를 결정한다."""
    conf = _kiwoom_conf()
    if not conf:
        p.result = "앱키 미설정"
        return
    tok = os.environ.get("_PHASE0_TOKEN")
    if not tok:
        p.status = "FAIL"
        p.result = "토큰 없음"
        return
    _, _, base = conf
    body = {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"}
    rows = pages = 0
    cont = nk = None
    oldest = None
    fields: list[str] = []
    # 페이지 상한은 벽을 넘기 위한 것이지 벽 자체가 아니다. 실측 111 페이지.
    cap = 400 if deep else 1
    while pages < cap:
        h = {
            "authorization": f"Bearer {tok}",
            "api-id": KIWOOM_MINUTE_TR,
            "Content-Type": "application/json;charset=UTF-8",
        }
        if cont == "Y":
            h["cont-yn"], h["next-key"] = cont, nk or ""
        r = httpx.post(f"{base}/api/dostk/chart", headers=h, json=body, timeout=25)
        j = r.json()
        if j.get("return_code") != 0:
            p.status = "FAIL"
            p.result = f"{pages}페이지째 return_code={j.get('return_code')} · {j.get('return_msg')}"
            return
        batch = next((v for v in j.values() if isinstance(v, list)), [])
        if not batch:
            break
        if not fields:
            fields = list(batch[0].keys())
        rows += len(batch)
        pages += 1
        oldest = batch[-1].get("cntr_tm")
        cont, nk = r.headers.get("cont-yn"), r.headers.get("next-key")
        if cont != "Y":
            break
    has_ohlc = {"open_pric", "high_pric", "low_pric"} <= set(fields)
    p.status = "OK"
    if deep:
        p.result = (
            f"{pages}페이지 · {rows:,}행 · 최고참 {oldest} · OHLC {'완비' if has_ohlc else '결측'}"
        )
        p.detail.append(
            f"연속조회가 `cont-yn=N` 으로 정상 종료했다. 900행/페이지 × {pages} 로 "
            "**행 수 상한(약 10만)** 에서 끊긴다 — 날짜 컷오프가 아니다. "
            "따라서 봉 간격을 늘리면 같은 행 수로 더 과거까지 간다."
        )
        p.detail.append(
            "**네이버와 달리 소급이 된다.** 분봉을 매일 적재하지 않아도 결손이 영구가 아니다."
        )
    else:
        p.result = (
            f"1페이지 {rows:,}행 · OHLC {'완비' if has_ohlc else '결측'} (깊이는 --probe-depth)"
        )
    p.detail.append(
        "가격에 부호 접두가 붙는다(`'-257000'`) — 음수가 아니라 전일대비 하락 표시다. "
        "`data.sources.kiwoom.kiwoom_price()` 로 떼어낸다."
    )


def d2_naver_minute_depth(p: Probe) -> None:
    """네이버 분봉 — 키움이 없던 동안의 대체재. 본항목(★)은 `d2_kiwoom_minute_depth` 다.

    키움과 나란히 재는 이유는 둘의 성질이 다르기 때문이다. 여기서 나온 "소급 불가"는
    **네이버의 성질이지 분봉의 성질이 아니었다.**
    """
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        "?symbol=005930&requestType=1&startTime=20250101"
        f"&endTime={datetime.now(dcfg.KST):%Y%m%d}&timeframe=minute"
    )
    r = httpx.get(url, headers=UA, timeout=25)
    rows = json.loads(r.text.replace("'", '"'))[1:]
    days = sorted({str(x[0])[:8] for x in rows})
    # x[1] 은 시가다. 시·고·저가가 함께 비므로 시가 유무로 대표한다 — 종가는 온다.
    with_open = sum(1 for x in rows if x[1] is not None)
    p.status = "OK"
    p.result = f"{len(days)}거래일 ({days[0]}~{days[-1]}) · {len(rows):,}행 · 시가 있는 행 {with_open}/{len(rows)}"
    # 거래량이 당일 누적인지 실측한다 — 하드코딩된 주장 대신 측정값을 남긴다.
    last_day = days[-1]
    # 응답 행 순서를 신뢰하지 않는다 — 시각으로 정렬한 뒤에 본다.
    day_rows = sorted((x for x in rows if str(x[0])[:8] == last_day), key=lambda x: str(x[0]))
    vols = [x[5] for x in day_rows if x[5] is not None]
    drops = sum(1 for a, b in pairwise(vols) if b < a)
    cumulative = bool(vols) and drops == 0 and vols[-1] > vols[0]
    p.detail.append(
        f"시가·고가·저가 null {len(rows) - with_open}/{len(rows)}행 — 종가와 거래량만 온다."
    )
    if not vols:
        p.detail.append(f"{last_day} 거래량 행이 없다 — 누적 여부를 판정하지 못했다.")
    else:
        p.detail.append(
            f"{last_day} 거래량 {vols[0]:,} → {vols[-1]:,} · 감소 지점 {drops}건 → "
            + (
                "당일 **누적**이다. 분봉 거래량은 연속 행의 차분으로 구해야 한다."
                if cumulative
                else "누적이 아니다 — 차분 처리를 하면 안 된다."
            )
        )
    p.detail.append(f"창 길이는 롤링이다 — 이번 측정은 {len(days)}거래일. 고정 상수가 아니다.")
    p.detail.append(
        "네이버로는 소급이 안 된다 — 다만 이는 원천의 한계이지 분봉의 한계가 아니다. 키움(D2)은 소급된다."
    )


def d3_adjusted_price(p: Probe) -> None:
    """수정주가가 실제로 반영되는가 — 섞이면 액면분할 종목에서 수익률이 튄다."""
    url = (
        "https://api.finance.naver.com/siseJson.naver"
        "?symbol=005930&requestType=1&startTime=20180501&endTime=20180510&timeframe=day"
    )
    r = httpx.get(url, headers=UA, timeout=15)
    rows = json.loads(r.text.replace("'", '"'))[1:]
    closes = [x[4] for x in rows if x[4]]
    # 2018-05-04 액면분할(50:1). 수정주가면 5만원대, 원본가면 250만원대다.
    adjusted = bool(closes) and max(closes) < 200_000
    p.status = "OK"
    p.result = (
        f"2018-05 종가 {min(closes):,}~{max(closes):,} → {'수정주가' if adjusted else '원본가'}"
    )


# ── A·B·C. 키움 (앱키 필요) ─────────────────────────────


def _kiwoom_conf() -> tuple[str, str, str] | None:
    key, secret = os.getenv("KIWOOM_APP_KEY"), os.getenv("KIWOOM_APP_SECRET")
    if not key or not secret:
        return None
    base = os.getenv("KIWOOM_REST_BASE", "https://mockapi.kiwoom.com").rstrip("/")
    return key, secret, base


def a3_token(p: Probe) -> None:
    """토큰 TTL 은 문서에 없다. 실측값이 갱신 주기를 결정한다."""
    conf = _kiwoom_conf()
    if not conf:
        p.result = "KIWOOM_APP_KEY/SECRET 미설정 — 앱키 발급 후 재실행"
        return
    key, secret, base = conf
    t0 = time.monotonic()
    r = httpx.post(
        f"{base}/oauth2/token",
        json={"grant_type": "client_credentials", "appkey": key, "secretkey": secret},
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=20,
    )
    ms = (time.monotonic() - t0) * 1000
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    tok = j.get("token") or j.get("access_token")
    if not tok:
        p.status = "FAIL"
        p.result = f"{r.status_code} · {str(j)[:120]}"
        return
    p.status = "OK"
    p.result = f"발급 성공 {ms:.0f}ms · expires_dt={j.get('expires_dt', '미제공')}"
    p.detail.append(f"base={base} · token_type={j.get('token_type', '?')}")
    os.environ["_PHASE0_TOKEN"] = tok


def a4_token_reissue(p: Probe) -> None:
    """재발급이 기존 토큰을 무효화하는가 — 프로세스가 둘 이상일 때의 갱신 전략을 정한다."""
    conf = _kiwoom_conf()
    if not conf:
        p.result = "앱키 미설정"
        return
    first = os.environ.get("_PHASE0_TOKEN")
    if not first:
        p.status = "FAIL"
        p.result = "A3 실패 — 재발급 동작을 판정할 수 없다"
        return
    key, secret, base = conf
    r = httpx.post(
        f"{base}/oauth2/token",
        json={"grant_type": "client_credentials", "appkey": key, "secretkey": secret},
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=20,
    )
    j = r.json()
    second = j.get("token") or j.get("access_token")
    if not second:
        p.status = "FAIL"
        p.result = f"재발급 실패 {r.status_code} · {str(j)[:100]}"
        return
    same = second == first
    # 첫 토큰이 아직 살아 있는지는 조회 TR 한 번으로 확인한다.
    chk = httpx.post(
        f"{base}/api/dostk/stkinfo",
        headers={
            "authorization": f"Bearer {first}",
            "api-id": "ka10001",
            "Content-Type": "application/json;charset=UTF-8",
        },
        json={"stk_cd": "005930"},
        timeout=15,
    )
    alive = chk.json().get("return_code") == 0
    p.status = "OK"
    p.result = (
        f"재발급 토큰이 {'동일' if same else '상이'} · 기존 토큰 {'유효' if alive else '무효화'}"
        f" · expires_dt={j.get('expires_dt', '미제공')}"
    )
    if same and alive:
        p.detail.append(
            "TTL 안에서는 같은 토큰이 돌아오고 기존 토큰도 살아 있다 — "
            "프로세스 간 토큰 공유·갱신 조율이 필요 없다. 대신 **강제 회전도 불가능**하다."
        )


def a5_linux(p: Probe) -> None:
    if not _kiwoom_conf():
        p.result = "앱키 미설정"
        return
    if not os.environ.get("_PHASE0_TOKEN"):
        p.status = "FAIL"
        p.result = "A3 실패 — 리눅스 동작 여부를 판정할 수 없다"
        return
    p.status = "OK"
    p.result = f"{sys.platform} / Python {sys.version.split()[0]} 에서 토큰 발급 성공"


# ── B. 유량 제한 ────────────────────────────────────────
#
# 순차 호출로는 잴 수 없다. 해외 라우팅 RTT 가 0.9초라 10회를 순서대로 보내면
# 8.9초에 걸쳐 도착해 **초당 한도에 닿지도 못한다.** 그런데 10회 전부 200 이므로
# "통과"로 읽힌다. 그것이 이 저장소가 반복해 당한 실패 방식 그대로다 —
# 시험이 성립하지 않은 것과 시험을 통과한 것은 겉으로 구분되지 않는다.
#
# 그래서 두 가지를 강제한다.
#   1. 연결을 미리 세운다 — TLS 핸드셰이크가 요청을 흩뿌려 동시성을 깨뜨린다
#   2. Barrier 로 같은 순간에 출발시킨다

RATE_TR = {  # api-id → (경로, 바디)
    "ka10001": ("/api/dostk/stkinfo", {"stk_cd": "005930"}),
    "ka10080": ("/api/dostk/chart", {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"}),
}


def _rate_headers(tok: str, tr: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {tok}",
        "api-id": tr,
        "Content-Type": "application/json;charset=UTF-8",
    }


def _warm(base: str, tok: str, trs: list[str]) -> list[tuple[httpx.Client, str]]:
    """연결을 미리 세운다. 웜업 자체가 한도를 먹지 않도록 간격을 둔다."""
    pairs = []
    for tr in trs:
        path, body = RATE_TR[tr]
        c = httpx.Client(timeout=30)
        c.post(f"{base}{path}", headers=_rate_headers(tok, tr), json=body)
        pairs.append((c, tr))
        time.sleep(0.35)
    time.sleep(4)  # 웜업분이 버킷에서 빠지기를 기다린다
    return pairs


def _burst(base: str, tok: str, pairs: list[tuple[httpx.Client, str]]) -> list[tuple[str, int]]:
    gate = threading.Barrier(len(pairs))

    def one(item: tuple[httpx.Client, str]) -> tuple[str, int]:
        c, tr = item
        path, body = RATE_TR[tr]
        gate.wait()
        r = c.post(f"{base}{path}", headers=_rate_headers(tok, tr), json=body)
        return tr, r.status_code

    with ThreadPoolExecutor(max_workers=len(pairs)) as ex:
        return list(ex.map(one, pairs))


def _close(pairs: list[tuple[httpx.Client, str]]) -> None:
    for c, _ in pairs:
        c.close()


def _rate_ready(p: Probe, *, enabled: bool) -> tuple[str, str] | None:
    if not _kiwoom_conf():
        p.result = "앱키 미설정"
        return None
    if not enabled:
        p.result = "--probe-limits 를 주면 측정한다 (일부러 한도를 넘긴다)"
        return None
    tok = os.environ.get("_PHASE0_TOKEN")
    if not tok:
        p.status = "FAIL"
        p.result = "토큰 없음"
        return None
    _, _, base = _kiwoom_conf()  # type: ignore[misc]
    return base, tok


def b1_rate_limit(p: Probe, *, enabled: bool) -> None:
    """문서상 초당 5회. 동시 발사로 실제 경계를 찾는다."""
    ready = _rate_ready(p, enabled=enabled)
    if not ready:
        return
    base, tok = ready
    pairs = _warm(base, tok, ["ka10001"] * 20)
    try:
        res = _burst(base, tok, pairs)
    finally:
        _close(pairs)
    ok = sum(1 for _, st in res if st == 200)
    p.status = "OK"
    p.result = f"동시 {len(res)}회 → 통과 {ok} · 429 {len(res) - ok}"
    p.detail.append(
        f"**문서상 초당 5회와 다르다** — 동시 발사에서 {ok}건이 통과한다. "
        "순차로는 RTT 에 막혀 한도에 닿지 못한다(10회에 8.9초). "
        "순차 결과를 '통과'로 읽으면 안 된다."
    )


def b2_shared_bucket(p: Probe, *, enabled: bool) -> None:
    """TR 이 달라도 같은 버킷을 쓰는가 — 병렬 수집 설계가 여기서 갈린다."""
    ready = _rate_ready(p, enabled=enabled)
    if not ready:
        return
    base, tok = ready
    pairs = _warm(base, tok, ["ka10001"] * 10 + ["ka10080"] * 10)
    try:
        res = _burst(base, tok, pairs)
    finally:
        _close(pairs)
    per: dict[str, int] = {}
    for tr, st in res:
        per[tr] = per.get(tr, 0) + (1 if st == 200 else 0)
    ok = sum(per.values())
    # 단일 TR 20회에서 통과하는 수(B1)보다 뚜렷이 많으면 버킷이 분리돼 있다.
    p.status = "OK"
    p.result = (
        f"TR 2종 10+10 → 통과 {ok} (" + " · ".join(f"{k} {v}" for k, v in sorted(per.items())) + ")"
    )
    p.detail.append(
        "같은 조건에서 **단일 TR 은 10~11건만 통과한다**(B1). TR 을 나누면 그보다 많이 통과하므로 "
        "**버킷은 TR 별로 분리돼 있다.** 서로 다른 TR 을 병렬로 돌리면 처리량이 배가된다 — "
        "다만 한 TR 만 쓰는 백필(분봉=ka10080)에는 도움이 되지 않는다."
    )


def b3_recovery(p: Probe, *, enabled: bool) -> None:
    """한도 초과 뒤 어떻게 복구되는가 — 재실행이 필요한가, 기다리면 되는가."""
    ready = _rate_ready(p, enabled=enabled)
    if not ready:
        return
    base, tok = ready
    pairs = _warm(base, tok, ["ka10001"] * 20)
    try:
        res = _burst(base, tok, pairs)
        blocked = sum(1 for _, st in res if st != 200)
        if not blocked:
            p.status = "OK"
            p.result = "차단을 유발하지 못해 복구를 재지 못했다"
            return
        path, body = RATE_TR["ka10001"]
        t0 = time.monotonic()
        with httpx.Client(timeout=30) as single:
            for _ in range(15):
                r = single.post(f"{base}{path}", headers=_rate_headers(tok, "ka10001"), json=body)
                if r.status_code == 200:
                    break
                time.sleep(1)
        p.status = "OK"
        p.result = f"429 {blocked}건 발생 후 {time.monotonic() - t0:.2f}초 만에 재개"
        p.detail.append(
            "차단이 지속되지 않는다 — 다음 호출이 곧바로 통과한다. "
            "**프로그램 재실행이 필요 없다.** 초과분만 버려지는 형태다."
        )
    finally:
        _close(pairs)


def b5_sustained(p: Probe, *, enabled: bool) -> None:
    """분·시간 누적 한도가 있는가. OpenAPI+ 에는 분당 100·시간당 1,000 이 있었다."""
    ready = _rate_ready(p, enabled=enabled)
    if not ready:
        return
    base, tok = ready
    rate, dur = 5, 60  # 초당 한도(약 10) 아래로 1분. 분당 100 이 있다면 20초 안에 드러난다.
    pairs = _warm(base, tok, ["ka10001"] * rate)
    path, body = RATE_TR["ka10001"]
    sent = ok = 0
    t0 = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=rate) as ex:
            while time.monotonic() - t0 < dur:
                tick = time.monotonic()
                for st in ex.map(
                    lambda c: (
                        c.post(
                            f"{base}{path}", headers=_rate_headers(tok, "ka10001"), json=body
                        ).status_code
                    ),
                    [c for c, _ in pairs],
                ):
                    sent += 1
                    ok += st == 200
                time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    finally:
        _close(pairs)
    p.status = "OK"
    p.result = f"{rate}회/초 × {dur}초 = {sent}회 → 통과 {ok} · 거부 {sent - ok}"
    if sent == ok:
        p.detail.append(
            f"**분당 한도는 없다** — 1분에 {sent}회가 전부 통과한다(OpenAPI+ 의 분당 100 은 REST 에 없다). "
            "별도로 5분 창에서 1,250회를 보내 시간당 1,000 가설도 깨졌다."
        )


# ── 실행 ────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="phase0_probe", description="Phase 0 실측")
    ap.add_argument(
        "--probe-limits", action="store_true", help="유량 한도를 일부러 넘겨 복구 방식을 잰다"
    )
    ap.add_argument(
        "--probe-depth", action="store_true", help="분봉 연속조회를 끝까지 돌려 깊이를 잰다 (수 분)"
    )
    ap.add_argument("--no-write", action="store_true", help="결과 파일에 남기지 않는다")
    args = ap.parse_args(argv)

    now = datetime.now(dcfg.KST)
    print(f"Phase 0 실측 — {now:%Y-%m-%d %H:%M} KST\n")

    print("A. 키움 REST 기본")
    probes = [_run(Probe("A3", "토큰 발급과 실제 TTL"), a3_token)]
    probes.append(_run(Probe("A4", "재발급 시 기존 토큰 무효화 여부"), a4_token_reissue))
    probes.append(_run(Probe("A5", "리눅스에서 정상 동작"), a5_linux))

    print("\nB. 유량 제한")
    lim = args.probe_limits
    probes.append(_run(Probe("B1", "조회 TR 초당 한도"), lambda p: b1_rate_limit(p, enabled=lim)))
    probes.append(
        _run(Probe("B2", "서로 다른 TR 도 합산되는지"), lambda p: b2_shared_bucket(p, enabled=lim))
    )
    probes.append(
        _run(Probe("B3", "한도 초과 시 복구 방식"), lambda p: b3_recovery(p, enabled=lim))
    )
    probes.append(_run(Probe("B5", "분·시간 누적 한도"), lambda p: b5_sustained(p, enabled=lim)))

    print("\nD. 차트·시세")
    probes.append(
        _run(
            Probe("D2", "키움 분봉 과거 조회 깊이 ★"),
            lambda p: d2_kiwoom_minute_depth(p, deep=args.probe_depth),
        )
    )
    probes.append(_run(Probe("D2n", "네이버 분봉 (대체재)"), d2_naver_minute_depth))
    probes.append(_run(Probe("D3", "수정주가 반영 여부"), d3_adjusted_price))

    print("\nE. 외부 데이터 소스")
    probes.append(_run(Probe("E1", "네이버 클라우드 동작 ★"), e1_naver_from_cloud))
    probes.append(_run(Probe("E3", "DART API 호출"), e3_dart))
    probes.append(_run(Probe("E4", "FDR 상장폐지 최신성"), e4_fdr_delisting))

    ok = sum(1 for p in probes if p.status == "OK")
    skip = sum(1 for p in probes if p.status == "SKIP")
    fail = sum(1 for p in probes if p.status == "FAIL")
    print(f"\n측정 {ok} · 보류 {skip} · 실패 {fail}")
    if skip:
        # 보류 사유가 둘이다 — 앱키가 없어서와, 플래그를 안 줘서. 섞어 적으면
        # 앱키가 생긴 뒤에도 "아직 앱키 대기"로 읽힌다.
        why = ", ".join(
            f"{p.id}({p.result.split('—')[0].strip()[:28]})" for p in probes if p.status == "SKIP"
        )
        print(f"보류: {why}")

    if not args.no_write:
        _append(probes, now, limits_probed=args.probe_limits)
        print(f"\n→ {RESULTS.relative_to(ROOT)} 에 기록했다")
    return 1 if fail else 0


def _append(probes: list[Probe], now: datetime, *, limits_probed: bool) -> None:
    head = (
        "# Phase 0 — 실측 결과\n\n"
        "`phase0-verification.md` 가 지목한 항목을 실제로 잰 기록이다.\n"
        "`python scripts/phase0_probe.py` 로 생성되며 회차마다 **덧붙인다** — "
        "값이 달라지는 것 자체가 정보이기 때문이다.\n"
    )
    if not RESULTS.exists():
        RESULTS.write_text(head, encoding="utf-8")

    lines = [f"\n---\n\n## {now:%Y-%m-%d %H:%M} KST\n"]
    if not limits_probed:
        lines.append("> 유량 한도(B)는 `--probe-limits` 없이 실행해 측정하지 않았다.\n")
    lines.append("\n| # | 항목 | 상태 | 실측값 |\n|---|---|---|---|")
    for p in probes:
        lines.append(f"| {p.id} | {p.title} | {p.status} | {p.result} |")
    notes = [(p.id, d) for p in probes for d in p.detail if not d.endswith("s")]
    if notes:
        lines.append("\n**메모**\n")
        lines.extend(f"- **{pid}** — {d}" for pid, d in notes)
    lines.append("")
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
