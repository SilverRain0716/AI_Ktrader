"""네이버 종목뉴스 — 유니버스에 오른 종목의 최근 기사 제목.

⚠️ 비공식이다. `naver.py` 와 같은 조건 — 약관상 근거가 없고 언제든 막힐 수 있다.
   - 호출 간격을 반드시 둔다 (`config.NAVER_REQUEST_INTERVAL_SEC`)
   - 실패를 정상 상황으로 취급하되 **조용히 빈 결과를 반환하지 않는다**

## 이 모듈이 하지 않는 것

**후보를 만들지 않는다.** 뉴스는 채널이 아니라 이미 선별된 종목의 컨텍스트다
([ADR 0010](../../docs/adr/0010-news-and-context.md)). 뉴스가 종목을 뽑으면 4번째 채널이 되고,
ADR 0006 이 F2 통과 전까지 막아 둔 대상이 된다.

**노이즈를 거르지 않는다.** 실측(2026-08-31): 종목 페이지 기사의 **40~85% 가 제목에
그 종목명을 담지 않는다**(가온전선 60% 포함 / HPSP 15% / GS건설 16%). 1페이지 4건이 전부
무관한 시황 기사인 경우도 있었다.

그래도 버리지 않는다 — 계열사·업종 기사로 재료가 나오는 경우가 있고(가온전선의 싱가포르
수주가 "LS전선" 계열 기사로도 돌았다), 무엇보다 **우리가 미리 버리면 무엇을 버렸는지
아무도 모른다**(11.6 의 교훈). 대신 `names_stock` 으로 표시해 AI 가 판단하게 한다.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from data import config

log = logging.getLogger("data.news")

NEWS_URL = "https://finance.naver.com/item/news_news.naver"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}

# 한 종목당 받을 페이지 수. 실측: 1페이지 12~40건, 3페이지면 대략 2~4주치다.
DEFAULT_PAGES = 2
# 팩에 실을 상한. **군집 후**라 3건이면 서로 다른 사건 3개다.
# 실측(2026-08-31): 8건으로 두니 뉴스만 18,376 토큰(건당 60)이 되어 팩이 상한을 넘었다.
# 건당 60 토큰 중 20 이 JSON 키 이름이다 — 항목 수를 줄이는 것이 가장 큰 지렛대다.
MAX_PER_STOCK = 3
NEWS_TIMEOUT_SEC = 20

_ROW = re.compile(
    r'<td class="title">.*?<a[^>]*>(.*?)</a>.*?class="info">(.*?)</td>.*?class="date">(.*?)</td>',
    re.S,
)
_TAG = re.compile(r"<[^>]+>")
# 제목 유사도 판정용 — 기호·공백·조사 흔들림을 없앤다
_NOISE = re.compile(r"[^0-9A-Za-z가-힣]+")


class NewsUnavailable(RuntimeError):
    """뉴스를 받을 수 없다. 호출자가 알아야 한다 — 빈 결과로 위장하지 않는다."""


@dataclass(frozen=True)
class NewsItem:
    headline: str
    at: str  # ISO 8601 (KST)
    source: str
    names_stock: bool
    cluster_size: int = 1

    def to_pack_item(self) -> dict:
        """기본값인 필드는 뺀다 — 키 이름만으로 건당 20 토큰이 나간다.

        `names_stock=false` 와 `cluster_size=1` 이 기본이다. 없으면 그 값으로 읽는다.
        """
        out: dict = {"headline": self.headline[:120], "at": self.at, "source": self.source[:20]}
        if self.names_stock:
            out["names_stock"] = True
        if self.cluster_size > 1:
            out["cluster_size"] = self.cluster_size
        return out


def _clean(raw: str) -> str:
    return htmllib.unescape(_TAG.sub("", raw)).strip()


def _parse_at(raw: str) -> str | None:
    """`2026.08.29 09:00` → ISO 8601. 못 읽으면 None — 추측하지 않는다."""
    try:
        dt = datetime.strptime(raw.strip(), "%Y.%m.%d %H:%M")
    except ValueError:
        return None
    # 초·오프셋을 뺀다. 14일 창 안의 기사에 초 단위는 의미가 없고 건당 4 토큰이다.
    return dt.strftime("%Y-%m-%dT%H:%M")


# 같은 사건 판정 임계. 실측(2026-08-31, 가온전선 8/25 DL이앤씨 건 10개 매체):
#   같은 사건끼리  0.13 ~ 0.42
#   무관한 기사   0.00
# 깔끔한 경계가 없다 — 한국어 조사 때문에 토큰이 흔들린다("DL이앤씨" vs "DL이앤씨와").
# 그래서 임계를 낮게 두되 **같은 날 안에서만** 묶는다. 날짜가 다르면 다른 사건이다.
SAME_EVENT_JACCARD = 0.12

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _tokens(headline: str) -> set[str]:
    return set(_TOKEN.findall(headline))


def _same_event(a: NewsItem, b: NewsItem) -> bool:
    if a.at[:10] != b.at[:10]:
        return False
    ta, tb = _tokens(a.headline), _tokens(b.headline)
    return len(ta & tb) / max(1, len(ta | tb)) >= SAME_EVENT_JACCARD


def fetch(
    code: str, name: str | None, *, pages: int = DEFAULT_PAGES, client=None
) -> list[NewsItem]:
    """한 종목의 최근 기사. **정렬·군집까지 마친 결과**를 돌려준다."""
    own = client is None
    client = client or httpx.Client(timeout=NEWS_TIMEOUT_SEC)
    raw: list[NewsItem] = []
    try:
        for page in range(1, pages + 1):
            r = client.get(NEWS_URL, params={"code": code, "page": page}, headers=UA)
            if r.status_code != 200:
                raise NewsUnavailable(f"{code}: HTTP {r.status_code}")
            for title, src, day in _ROW.findall(r.text):
                headline, at = _clean(title), _parse_at(_clean(day))
                if not headline or not at:
                    continue
                raw.append(
                    NewsItem(
                        headline=headline,
                        at=at,
                        source=_clean(src),
                        names_stock=bool(name) and name in headline,
                    )
                )
            time.sleep(config.NAVER_REQUEST_INTERVAL_SEC)
    finally:
        if own:
            client.close()
    return _cluster(raw)


def _cluster(items: list[NewsItem]) -> list[NewsItem]:
    """같은 사건을 하나로 묶되 **몇 매체가 썼는지를 남긴다.**

    중복 제거가 아니라 신호다 — 10개 매체가 쓴 사건은 1개보다 크다.

    묶지 않으면 상한(8건)이 **한 사건으로 다 찬다.** 실측에서 가온전선의 8/25 건이
    10개 매체로 나와 다른 재료를 전부 밀어냈다.
    """
    groups: list[list[NewsItem]] = []
    for it in sorted(items, key=lambda x: x.at):
        for g in groups:
            if _same_event(g[0], it):
                g.append(it)
                break
        else:
            groups.append([it])

    out = [
        NewsItem(
            headline=g[0].headline,  # 가장 이른 기사가 사건 시각에 가깝다
            at=g[0].at,
            source=g[0].source,
            # 한 매체라도 종목명을 달았으면 그 사건은 그 종목 건이다
            names_stock=any(m.names_stock for m in g),
            cluster_size=len(g),
        )
        for g in groups
    ]
    # **최신순으로만 자르면 상한이 전부 노이즈로 찬다.** 실측: 가온전선 상위 8건이
    # 모두 무관한 시황 기사였고 정작 그 종목 재료는 뒤로 밀렸다.
    # 거르지는 않되(ADR 0010) **종목명이 달린 기사를 먼저 보여준다** — 상한이 있는 이상
    # 무엇을 먼저 둘지는 정해야 하고, 그 규칙이 보이지 않으면 아무도 모른다.
    return sorted(out, key=lambda x: (x.names_stock, x.at), reverse=True)


def for_universe(
    codes_names: list[tuple[str, str | None]],
    *,
    as_of_iso: str,
    pages: int = DEFAULT_PAGES,
    limit: int = MAX_PER_STOCK,
    client=None,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """유니버스 종목들의 뉴스. 실패는 사유와 함께 돌려준다.

    **`as_of_iso` 보다 나중에 발행된 기사는 버린다.** 11.9 에서 브리핑 채널이 미래 정보를
    읽었던 사고가 그대로 재현될 수 있고, 다음 단계가 백테스트 리플레이다.
    """
    own = client is None
    client = client or httpx.Client(timeout=NEWS_TIMEOUT_SEC)
    ok: dict[str, list[dict]] = {}
    failed: dict[str, str] = {}
    try:
        for code, name in codes_names:
            try:
                items = fetch(code, name, pages=pages, client=client)
            except Exception as e:  # 개별 실패가 전체를 멈추지 않는다
                failed[code] = f"{type(e).__name__}: {e}"
                continue
            fresh = [i for i in items if i.at <= as_of_iso]
            ok[code] = [i.to_pack_item() for i in fresh[:limit]]
    finally:
        if own:
            client.close()
    return ok, failed


__all__ = ["MAX_PER_STOCK", "NewsItem", "NewsUnavailable", "fetch", "for_universe"]
