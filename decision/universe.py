"""유니버스 선정 — 2단계 축소.

AI는 여기 없는 종목을 신규 진입 대상으로 고를 수 없다. 환각을 막는 장치이자
동시에 시야의 한계다. 그래서 "무엇을 넣을지"가 이 시스템에서 가장 중요한 규칙이다.

1단계 하드 필터  : 거래 불가·관리종목·유동성 부족·규모 미달을 잘라낸다
2단계 3채널 랭킹 : 성격이 다른 세 경로에서 각각 뽑아 합집합을 만든다

**단일 점수로 정렬하지 않는다.** 하나의 스코어로 줄을 세우면 그 스코어가 좋아하는
한 가지 성격의 종목만 상위를 채우고, AI는 서로 다른 근거를 비교할 기회를 잃는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime

from data import config as dcfg
from decision import config

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    code: str
    name: str | None = None
    market: str | None = None
    sector: str | None = None
    indicators: dict = field(default_factory=dict)
    flows: dict = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    screen_reasons: list[str] = field(default_factory=list)

    def to_pack_item(self) -> dict:
        return _compact(
            {
                "code": self.code,
                "name": self.name,
                "market": self.market if self.market in ("KOSPI", "KOSDAQ") else None,
                "sector": self.sector,
                "indicators": _round_indicators(self.indicators),
                "flows": _round_flows(self.flows),
                "tradable": True,  # 하드 필터를 통과했다는 뜻
                "screen_reasons": self.screen_reasons,
                "channels": self.channels,
            }
        )


# ── 컨텍스트 절약 ───────────────────────────────────────
# 종목 하나가 821자(≈313토큰)나 됐다. 60종목이면 유니버스만 18,000토큰으로
# 팩 상한에 육박한다. 원인은 과도한 소수점(1167933.3333)과 중복 필드였다.
# AI 판단에 소수점 4자리는 아무 의미가 없고, 주의만 분산시킨다.

# 이동평균 원값은 싣지 않는다 — ma_aligned(정배열 여부)와 disparity20_pct(이격도)로 충분하다.
_DROP_INDICATORS = ("ma5", "ma20", "ma60")
# 가격 계열은 정수, 나머지는 소수점 둘째 자리
_INT_FIELDS = ("close", "atr14", "macd_hist")


def _round_indicators(ind: dict) -> dict:
    out: dict = {}
    for k, v in ind.items():
        if k in _DROP_INDICATORS or v is None:
            continue
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = round(v) if k in _INT_FIELDS else round(float(v), 2)
        else:
            out[k] = v
    return out


def _round_flows(flows: dict) -> dict:
    out: dict = {}
    for k, v in flows.items():
        if v is None or k == "as_of":  # as_of 는 data_quality.flows_as_of 에 이미 있다
            continue
        out[k] = round(float(v), 2) if isinstance(v, float) else v
    return out


def _compact(d: dict) -> dict:
    """null 과 빈 컬렉션을 뺀다. 스키마상 전부 선택 필드다."""
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


@dataclass
class UniverseResult:
    candidates: list[Candidate]
    hard_filter_passed: int
    warnings: list[str] = field(default_factory=list)


# ── 1단계: 하드 필터 ────────────────────────────────────


def hard_filter(
    conn: sqlite3.Connection, as_of: date, *, now: datetime | None = None
) -> dict[str, Candidate]:
    """거래 자체가 불가능하거나 슬리피지가 전략을 삼키는 종목을 잘라낸다."""
    halt_since = (as_of - timedelta(days=config.HALT_LOOKBACK_DAYS)).isoformat()

    rows = conn.execute(
        """
        SELECT l.code, l.name, l.market, l.sector_group, i.payload
        FROM listing l
        JOIN indicators i ON i.code = l.code
        WHERE l.is_preferred = 0
          AND l.is_spac = 0
          AND l.is_managed = 0
          -- 시점 상한이 없으면 과거 시점 재현에서 미래 지표가 들어온다 (치명 C).
          AND i.date = (SELECT MAX(date) FROM indicators WHERE code = l.code AND date <= ?)
          AND l.code NOT IN (SELECT DISTINCT code FROM delisting)
          AND l.code NOT IN (
              -- 상한이 없으면 '나중에 정지될 종목'을 미리 피하는 완벽한 미래 정보가 된다.
              SELECT DISTINCT code FROM ohlcv WHERE halted = 1 AND date >= ? AND date <= ?
          )
          -- 배제는 기간 제한 없이 영구다(사용자 확정 정책). 되돌아올 길이 없으므로
          -- 카테고리가 아니라 dart.is_disqualifying() 이 방향까지 본 판정을 쓴다 —
          -- `불성실공시법인미지정`, `상장적격성 실질심사 대상 제외 결정` 처럼
          -- 같은 카테고리 안에 해소 공시가 섞여 있어 오탐 한 건이 종목을 영원히 배제한다.
          -- 영구라도 해소 공시(대상 제외 결정·법인 미지정)가 더 나중이면 배제를 푼다.
          -- 그러지 않으면 이미 정상화된 종목이 과거 한 건 때문에 영원히 빠진다.
          -- 배제 집합의 크기는 공시를 언제부터 적재했는가에 달려 있다.
          -- 그 시작일은 data_quality.disclosures_since 로 팩에 실린다.
          AND l.code NOT IN (
              SELECT d.code FROM disclosures d
              WHERE d.code IS NOT NULL AND d.disqualifying = 1 AND d.rcept_dt <= ?
              GROUP BY d.code
              HAVING MAX(d.rcept_dt) > COALESCE(
                  (SELECT MAX(r.rcept_dt) FROM disclosures r
                   WHERE r.code = d.code AND r.resolving = 1 AND r.rcept_dt <= ?), '')
          )
        """,
        (
            as_of.isoformat(),
            halt_since,
            as_of.isoformat(),
            as_of.strftime("%Y%m%d"),
            as_of.strftime("%Y%m%d"),
        ),
    ).fetchall()

    out: dict[str, Candidate] = {}
    for code, name, market, sector, payload in rows:
        try:
            p = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        ind = p.get("indicators") or {}
        flows = p.get("flows") or {}

        if (p.get("bars") or 0) < config.MIN_BARS:
            continue
        adv = ind.get("adv20_eok_krw")
        cap = ind.get("market_cap_eok_krw")
        if adv is None or adv < config.MIN_ADV20_EOK_KRW:
            continue
        if cap is None or cap < config.MIN_MARKET_CAP_EOK_KRW:
            continue

        out[code] = Candidate(
            code=code, name=name, market=market, sector=sector, indicators=ind, flows=flows
        )
    return out


# ── 2단계: 3채널 랭킹 ───────────────────────────────────


def _briefing_channel(
    conn: sqlite3.Connection, pool: dict[str, Candidate], as_of: date, quota: int, now: datetime
) -> list[tuple[str, str]]:
    """최근 브리핑에서 주목·조건부 관점을 받은 종목.

    ⚠️ 브리핑은 '이미 오른 종목'을 사후 설명하는 구조다(상승률·거래대금 상위에서 선정).
    이 채널만 두면 추격매수 편향이 그대로 들어온다. 그래서 모멘텀·수급 채널을 나란히 둔다.
    """
    since = (as_of - timedelta(days=config.BRIEFING_LOOKBACK_DAYS)).isoformat()
    order = {"상": 0, "중상": 1, "중": 2, "중하": 3, "하": 4, None: 5}
    rows = conn.execute(
        f"""SELECT v.code, v.stance, v.confidence, v.day, v.kind
            FROM briefing_views v JOIN briefings b ON b.briefing_id = v.briefing_id
            WHERE v.market='KR' AND v.code IS NOT NULL AND v.day >= ?
              -- 상한이 없으면 아직 발행되지 않은 브리핑으로 종목을 뽑는다.
              -- pack.py 의 브리핑 블록은 이 상한을 걸고 있었으므로, 팩에는 없는 브리핑을
              -- 근거로 유니버스에 오르는 모순이 생겼다 (점검 2026-08-23 치명 B).
              AND b.published_at <= ?
              AND v.stance IN ({",".join("?" * len(config.BRIEFING_STANCES))})
            ORDER BY v.day DESC""",
        (since, now.isoformat(timespec="seconds"), *config.BRIEFING_STANCES),
    ).fetchall()

    best: dict[str, tuple] = {}
    for code, stance, conf, day, kind in rows:
        if code not in pool:
            continue
        key = (order.get(conf, 5), -int(day.replace("-", "")))
        if code not in best or key < best[code][0]:
            best[code] = (key, f"briefing:{kind} {stance}/{conf or '확신도없음'} ({day})")
    ranked = sorted(best.items(), key=lambda kv: kv[1][0])[:quota]
    return [(code, reason) for code, (_, reason) in ranked]


def _momentum_channel(pool: dict[str, Candidate], quota: int) -> list[tuple[str, str]]:
    """시장 대비 강한 추세. 정배열 + RSI 과열 전 구간에서 상대강도 상위."""
    lo, hi = config.MOMENTUM_RSI_RANGE
    scored = []
    for c in pool.values():
        i = c.indicators
        if not i.get("ma_aligned"):
            continue
        rsi = i.get("rsi14")
        rs = i.get("rs20")
        if rsi is None or rs is None or not (lo <= rsi <= hi):
            continue
        scored.append((rs, c.code, f"momentum: 정배열 RSI {rsi:.0f} RS20 {rs:+.1f}%p"))
    scored.sort(key=lambda x: -x[0])
    return [(code, reason) for _, code, reason in scored[:quota]]


def _flow_channel(pool: dict[str, Candidate], quota: int) -> list[tuple[str, str]]:
    """서사보다 앞서는 자금 흐름. 연속 순매수 + 시총 대비 순매수 강도."""
    scored = []
    for c in pool.values():
        f = c.flows
        fd = f.get("foreign_net_days") or 0
        idd = f.get("inst_net_days") or 0
        if max(fd, idd) < config.FLOW_MIN_NET_DAYS:
            continue
        cap = c.indicators.get("market_cap_eok_krw")
        if not cap:
            continue
        net = (f.get("foreign_net_5d_eok_krw") or 0) + (f.get("inst_net_5d_eok_krw") or 0)
        if net <= 0:
            continue
        intensity = net / cap * 100
        scored.append(
            (
                intensity,
                c.code,
                f"flow: 외{fd}일/기{idd}일 연속, 5일 순매수 시총대비 {intensity:.2f}%",
            )
        )
    scored.sort(key=lambda x: -x[0])
    return [(code, reason) for _, code, reason in scored[:quota]]


def build(
    conn: sqlite3.Connection,
    as_of: date,
    *,
    now: datetime | None = None,
    exclude: set[str] | None = None,
) -> UniverseResult:
    """하드 필터 → 3채널 랭킹 → 합집합.

    `now` 는 "지금 몇 시인가" — 브리핑처럼 하루 안에서도 시점이 갈리는 입력의 상한이다.
    주지 않으면 as_of 의 장 마감(23:59)으로 둔다.
    """
    if now is None:
        now = datetime.combine(as_of, dtime(23, 59, 59), tzinfo=dcfg.KST)
    pool = hard_filter(conn, as_of, now=now)
    warnings: list[str] = []
    passed = len(pool)

    if exclude:
        for code in exclude:
            pool.pop(code, None)

    if passed < config.RANKING_MEANINGFUL_THRESHOLD:
        warnings.append(
            f"하드 필터 통과 {passed}종목 — {config.RANKING_MEANINGFUL_THRESHOLD}개 미만이라 "
            "3채널 랭킹이 사실상 전수 통과가 된다. 임계값 재검토 필요"
        )

    picks: dict[str, Candidate] = {}
    for channel, fn in (
        ("briefing", lambda q: _briefing_channel(conn, pool, as_of, q, now)),
        ("momentum", lambda q: _momentum_channel(pool, q)),
        ("flow", lambda q: _flow_channel(pool, q)),
    ):
        for code, reason in fn(config.CHANNEL_QUOTA[channel]):
            c = picks.setdefault(code, pool[code])
            if channel not in c.channels:
                c.channels.append(channel)
                c.screen_reasons.append(reason)

    # 상한 초과 시 채널 수가 많은 종목(여러 근거가 겹치는 종목)을 우선 남긴다
    cands = sorted(picks.values(), key=lambda c: (-len(c.channels), c.code))[: config.UNIVERSE_MAX]
    return UniverseResult(candidates=cands, hard_filter_passed=passed, warnings=warnings)
