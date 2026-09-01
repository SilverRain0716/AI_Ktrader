"""키움 REST 데이터 소스 — 장중 현재가.

## 왜 필요한가

판단 계층에는 외부 시세 호출 경로가 없었다. 팩의 가격은 `ohlcv` 테이블의 최신 종가이고
적재 배치는 18:30 에 돈다. 그래서 **12:20·15:00 사이클이 전 거래일 종가만 보고 있었다** —
설계안 v1 4.3 이 12:20 에 요구한 것은 "오전 흐름 반영"인데 구현이 그것을 할 수 없었다
([ADR 0009](../../docs/adr/0009-entry-timing.md) 맥락).

## 두 가지 함정

1. **가격에 부호 접두가 붙는다.** `'-257000'` 은 음수가 아니라 **전일대비 하락 표시**다.
   그대로 `int()` 하면 가격이 음수가 되고 수익률·지표가 조용히 뒤집힌다.
   `kiwoom_price()` 가 뗀다 — 예전에는 이 함수가 `scripts/phase0_probe.py` 에만 있었고,
   `CLAUDE.md` 가 "적재 경로에 키움 소스가 생기면 여기로 옮겨야 한다"고 적어 뒀다. 지금이 그 시점이다.
2. **네이버 일봉은 수정주가, 키움 현재가는 원본가다.** 두 축척을 섞으면 액면분할 종목에서
   수익률이 튄다(`CLAUDE.md` 데이터 계층 함정). 당일 가격끼리는 문제가 없지만
   **당일 현재가 ÷ 전일 수정종가**는 분할 당일에 틀린다 — 그래서 `SpotQuote` 가
   키움이 준 전일종가(`prev_close`)를 함께 들고 오고, 등락률은 **같은 축척 안에서** 계산한다.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from data import config

log = logging.getLogger("data.kiwoom")

TOKEN_URL = "/oauth2/token"
DAILY_CHART_TR = "ka10081"  # 주식일봉차트조회 — 통합(_AL)·수정주가 지원, 600건 소급
DAILY_CHART_PATH = "/api/dostk/chart"
STOCK_INFO_TR = "ka10001"  # 주식기본정보요청
STOCK_INFO_PATH = "/api/dostk/stkinfo"

# 유량은 **실전과 모의가 다르다.** 실측(2026-08-30):
#
#   실전  동시 발사 통과 10~11건 · 5회/초 지속 300회 전부 통과 · 분·시간 누적 한도 없음
#   모의  동시 발사 통과 **3건 고정**(6에서도 3, 12에서도 3) · 지속은 **약 2콜/초 상한**
#         (3회/초·4회/초 모두 실효 2.0 으로 수렴)
#
# 문서는 모의를 "TR당 초당 1회"라고 적었는데 실측은 그보다 관대하다. 그래도 실전의
# 약 1/3.5 다 — 실전 기준 동시성을 모의에 그대로 쓰면 8건 중 5~6건이 429 가 된다.
# **제약은 동시성이 아니라 속도다.** 동시 2 라도 RTT 가 0.7초면 초당 2.8회가 나가
# 모의의 지속 상한 2 를 넘는다 — 실제로 그렇게 해서 20종목 중 1종목이 429 였다.
# 그래서 모의에서는 호출 사이 최소 간격을 강제한다(2콜/초 = 0.5초).
MAX_CONCURRENCY = 8
MIN_INTERVAL_SEC = 0.0  # 실전은 동시성으로만 제어한다

MOCK_MAX_CONCURRENCY = 2
MOCK_MIN_INTERVAL_SEC = 0.55  # 2콜/초에 여유를 조금 둔다

MOCK_HOST_MARK = "mockapi"

# 429 는 지속되지 않는다 — 실측(2026-08-30 B3): 다음 호출이 0.9초(RTT 1회분) 만에 통과했다.
# 하드 게이트가 아니라 짧은 재시도로 충분하다는 뜻이다.
RETRY_ON_429 = 2
RETRY_SLEEP_SEC = 1.0

# 토큰 TTL 은 24시간이고 재발급해도 같은 토큰이 온다(실측). 강제 회전은 불가능하다.
_TOKEN_MARGIN = timedelta(minutes=30)


class KiwoomUnavailable(RuntimeError):
    """키움을 부를 수 없다. **조용히 빈 결과를 반환하지 않는다** — 호출자가 알아야 한다."""


def kiwoom_price(raw: str | int | None) -> int | None:
    """키움 가격의 부호 접두를 떼어낸다.

    `'-257000'` 은 **음수가 아니라 전일대비 하락 표시**다.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    return abs(int(text.lstrip("+-")))


# 우리가 계산한 등락률과 거래소가 준 등락률이 이보다 벌어지면 파싱이 틀린 것이다.
# 부호 접두를 안 뗀 사고가 정확히 이 검사에 걸린다.
CHANGE_PCT_TOLERANCE = 0.05


@dataclass(frozen=True)
class SpotQuote:
    """한 종목의 장중 시세. 등락률은 **같은 축척 안에서** 계산된 값이다."""

    code: str
    price: int
    prev_close: int | None  # 키움 base_pric (전일 기준가)
    reported_change_pct: float | None  # 키움 flu_rt — 우리 계산의 대조군
    as_of: str  # 조회 시각 (ISO 8601, KST)

    @property
    def change_pct(self) -> float | None:
        if not self.prev_close:
            return self.reported_change_pct
        return round((self.price - self.prev_close) / self.prev_close * 100, 2)

    @property
    def consistent(self) -> bool:
        """우리 계산이 거래소 값과 맞는가.

        맞지 않으면 값을 쓰지 않는다 — **틀린 가격은 없는 가격보다 나쁘다.**
        실측(2026-08-30 삼성전자): (257000−266000)/266000 = −3.38% = flu_rt.
        """
        mine, theirs = self.change_pct, self.reported_change_pct
        if mine is None or theirs is None:
            return True  # 대조할 것이 없으면 판정하지 않는다
        return abs(mine - theirs) <= CHANGE_PCT_TOLERANCE


class KiwoomClient:
    """토큰을 재사용하는 얇은 클라이언트. 스레드 안전하다."""

    def __init__(self, base: str | None = None, http=None):
        self.base = (base or os.getenv("KIWOOM_REST_BASE") or "").rstrip("/")
        # 모의 서버는 유량이 다르다. 엔드포인트로 판정한다 — KIWOOM_ENV 는 읽는 코드가
        # 따로 없어 믿을 수 없고, 실제로 어디에 쏘는지는 base 가 정한다.
        self.is_mock = MOCK_HOST_MARK in self.base
        self.max_workers = MOCK_MAX_CONCURRENCY if self.is_mock else MAX_CONCURRENCY
        self.min_interval = MOCK_MIN_INTERVAL_SEC if self.is_mock else MIN_INTERVAL_SEC
        self._pace_lock = threading.Lock()
        self._last_call = 0.0
        self._http = http
        self._token: str | None = None
        self._expires: datetime | None = None
        self._lock = threading.Lock()

    # ── 인증 ────────────────────────────────────────────

    def _client(self):
        # lock 없이 lazy init 하면 스레드마다 다른 클라이언트가 생겨 연결 풀이 갈라진다.
        if self._http is None:
            with self._lock:
                if self._http is None:
                    self._http = httpx.Client(timeout=20)
        return self._http

    def token(self) -> str:
        http = self._client()  # lock 밖에서 — token() 이 같은 lock 을 다시 잡는다
        with self._lock:
            now = datetime.now(config.KST)
            if self._token and self._expires and now < self._expires - _TOKEN_MARGIN:
                return self._token
            key, secret = os.getenv("KIWOOM_APP_KEY"), os.getenv("KIWOOM_APP_SECRET")
            if not (key and secret and self.base):
                raise KiwoomUnavailable(
                    "KIWOOM_APP_KEY / KIWOOM_APP_SECRET / KIWOOM_REST_BASE 가 필요하다."
                )
            r = http.post(
                f"{self.base}{TOKEN_URL}",
                json={"grant_type": "client_credentials", "appkey": key, "secretkey": secret},
                headers={"Content-Type": "application/json;charset=UTF-8"},
            )
            j = r.json()
            tok = j.get("token") or j.get("access_token")
            if not tok:
                raise KiwoomUnavailable(f"토큰 발급 실패 — {str(j)[:120]}")
            self._token = tok
            self._expires = _parse_expires(j.get("expires_dt")) or now + timedelta(hours=12)
            return tok

    def _pace(self) -> None:
        """호출 사이 최소 간격을 지킨다. 간격이 0 이면 아무것도 하지 않는다."""
        if not self.min_interval:
            return
        with self._pace_lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    # ── 범용 호출 ───────────────────────────────────────

    def post(self, tr: str, path: str, body: dict) -> dict:
        """TR 하나를 부른다. **유량·재시도·페이싱을 `spot()` 과 똑같이 태운다.**

        따로 만들면 한쪽만 페이싱을 타서 모의 서버에서 429 가 난다 —
        실제로 그 실수를 한 적이 있다(동시성만 낮추고 간격을 안 둬서 20종목 중 1건 429).
        """
        for attempt in range(RETRY_ON_429 + 1):
            self._pace()
            r = self._client().post(
                f"{self.base}{path}",
                headers={
                    "authorization": f"Bearer {self.token()}",
                    "api-id": tr,
                    "Content-Type": "application/json;charset=UTF-8",
                },
                json=body,
            )
            if r.status_code != 429:
                break
            if attempt == RETRY_ON_429:
                raise KiwoomUnavailable(f"{tr}: 유량 한도 초과 ({attempt + 1}회 시도)")
            time.sleep(RETRY_SLEEP_SEC)
        j = r.json()
        if j.get("return_code") not in (0, None):
            raise KiwoomUnavailable(f"{tr}: {j.get('return_msg')}")
        return j

    # ── 일봉 (통합 거래소) ──────────────────────────────

    def daily_chart(
        self, code: str, *, base_dt: str, venue: str = "AL", adjusted: bool = True
    ) -> list[dict]:
        """일봉. **`venue='AL'` 이 통합(KRX+NXT)이다.**

        네이버 일봉은 KRX 만 담고 그것도 약간 적다. 실측(2026-09-01, 24종목):
        **우리 DB / 통합 = 중앙 75% · 최소 35%.** NXT 비중이 종목마다 0~65% 로 갈려서
        단순 배율로 보정할 수 없다 — 종목 간 거래대금 비교가 통째로 왜곡된다.

        `adjusted=True` 가 네이버와 같은 축척이다. 실측: 가온전선이 원본가와는
        224/267 불일치인데 **수정주가와는 0/267 일치**한다.

        `trde_prica`(거래대금, 백만원)를 그대로 쓴다 — `종가 × 거래량` 근사보다 정확하다.
        """
        suffix = "" if venue == "KRX" else f"_{venue}"
        j = self.post(
            DAILY_CHART_TR,
            DAILY_CHART_PATH,
            {
                "stk_cd": f"{code}{suffix}",
                "base_dt": base_dt,
                "upd_stkpc_tp": "1" if adjusted else "0",
            },
        )
        out = []
        for d in j.get("stk_dt_pole_chart_qry") or []:
            o, h, low, c = (
                kiwoom_price(d.get(k)) for k in ("open_pric", "high_pric", "low_pric", "cur_prc")
            )
            vol = int(d.get("trde_qty") or 0)
            if not all((o, h, low, c)) or vol <= 0:
                # 0 값 행은 거래정지일이거나 **아직 거래가 없는 당일**이다.
                # 장 시작 전에 돌리면 ka10081 이 당일 행을 거래량 0 으로 주는데
                # (실측 2026-09-01 06:15: 668종목 전부 OHLC 동일·거래량 0),
                # 그것을 halted=0 으로 넣으면 "정지 아닌데 거래량 0" 이라는 모순이 남는다.
                continue
            out.append(
                {
                    "date": f"{d['dt'][:4]}-{d['dt'][4:6]}-{d['dt'][6:]}",
                    "open": o,
                    "high": h,
                    "low": low,
                    "close": c,
                    "volume": vol,
                    # 백만원 단위. 억원으로 바꿔 두면 저장소 관례(_eok_krw)와 맞는다
                    "value_eok": float(d.get("trde_prica") or 0) / 100,
                }
            )
        return out

    # ── 현재가 ──────────────────────────────────────────

    def spot(self, code: str) -> SpotQuote | None:
        """한 종목의 현재가. 응답이 이상하면 **None 이 아니라 예외**로 알린다."""
        for attempt in range(RETRY_ON_429 + 1):
            self._pace()
            r = self._client().post(
                f"{self.base}{STOCK_INFO_PATH}",
                headers={
                    "authorization": f"Bearer {self.token()}",
                    "api-id": STOCK_INFO_TR,
                    "Content-Type": "application/json;charset=UTF-8",
                },
                json={"stk_cd": code},
            )
            if r.status_code != 429:
                break
            if attempt == RETRY_ON_429:
                raise KiwoomUnavailable(f"{code}: 유량 한도 초과 ({attempt + 1}회 시도)")
            time.sleep(RETRY_SLEEP_SEC)
        j = r.json()
        if j.get("return_code") != 0:
            raise KiwoomUnavailable(f"{code}: {j.get('return_msg')}")
        price = kiwoom_price(j.get("cur_prc"))
        if not price:
            return None  # 거래정지·상장폐지 등 — 값이 없는 것은 정상 상황이다
        quote = SpotQuote(
            code=code,
            price=price,
            prev_close=kiwoom_price(j.get("base_pric")),
            reported_change_pct=_float_or_none(j.get("flu_rt")),
            as_of=datetime.now(config.KST).isoformat(timespec="seconds"),
        )
        if not quote.consistent:
            raise KiwoomUnavailable(
                f"{code}: 등락률 불일치 — 우리 {quote.change_pct} vs 거래소 "
                f"{quote.reported_change_pct}. 가격 파싱이 틀렸다."
            )
        return quote

    def spots(self, codes: list[str]) -> tuple[dict[str, SpotQuote], dict[str, str]]:
        """여러 종목을 동시에. **실패한 종목을 사유와 함께 돌려준다.**

        일부만 받아 놓고 전체인 척하면 팩이 "일부는 장중가, 일부는 전일 종가"인
        섞인 상태가 되고, 그것이 겉으로 드러나지 않는다.

        사유를 삼키지 않는 이유: 처음에 `log.debug` 로 흘렸더니 20종목 중 6종목이
        실패하는데 **왜인지 알 수 없었다.** 실패율만 보이고 원인이 안 보이면 고칠 수 없다.
        """
        ok: dict[str, SpotQuote] = {}
        failed: dict[str, str] = {}

        def one(code: str) -> None:
            try:
                q = self.spot(code)
            except Exception as e:  # 개별 실패는 전체를 멈추지 않는다
                failed[code] = f"{type(e).__name__}: {e}"
                return
            if q:
                ok[code] = q
            else:
                failed[code] = "현재가 없음 (거래정지·상장폐지 등)"

        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(codes)))) as ex:
            list(ex.map(one, codes))
        return ok, failed

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


def _float_or_none(raw) -> float | None:
    text = str(raw or "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _parse_expires(raw) -> datetime | None:
    """`expires_dt` 는 `YYYYMMDDHHMMSS` 다. 못 읽으면 None — 추측하지 않는다."""
    text = str(raw or "").strip()
    if len(text) != 14 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=config.KST)
    except ValueError:
        return None


def fetch_spots(codes: list[str], *, client: KiwoomClient | None = None):
    """편의 함수. 자격증명이 없으면 `KiwoomUnavailable` 이 그대로 올라간다."""
    own = client is None
    client = client or KiwoomClient()
    try:
        client.token()  # 자격증명을 여기서 확정한다 — 루프 안에서 새면 원인이 가려진다
        return client.spots(codes)
    finally:
        if own:
            client.close()


__all__ = [
    "KiwoomClient",
    "KiwoomUnavailable",
    "SpotQuote",
    "fetch_spots",
    "kiwoom_price",
]
