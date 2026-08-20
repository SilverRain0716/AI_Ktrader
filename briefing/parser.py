"""브리핑 원문(.md) → 구조화 JSON.

설계 원칙
- **원문 섹션을 통째로 보존한다.** 지수·수급 수치는 파싱하지 않는다.
  컨텍스트 팩은 그 값을 데이터 계층이 직접 계산한 것으로 쓴다(자유 텍스트에서 숫자를 긁으면 오독이 섞인다).
  나중에 필요해지면 보존된 원문에서 재추출하면 되고, GitLab을 다시 뒤질 필요가 없다.
- **조용히 버리지 않는다.** 코드 매핑 실패, 근거 부족, 틀리는 조건 누락은 전부 parse_warnings에 남긴다.
  경고가 쌓이면 브리핑 생성 프롬프트나 파서 중 하나가 어긋났다는 신호다.

종목 블록은 브리핑 종류마다 표기가 다르다 (실측 5가지 변형).
    kr-close-deep      [1] SK하이닉스(000660) 169만1,000원 +12.73%
                       · 관점: 주목 / 확신도 중상
    kr-premarket-deep  [1] SK하이닉스(000660) — 관점: 조건부 / 확신도 중
    kr-preclose        1. SK하이닉스 (+12%대) — 설명            ← 종목코드 없음
                        관점: 주목 / 확신도 중상 / 틀리는 조건: … / 확인: …
    us-close           1) 모더나(MRNA) 종가 +약100%
                       · 관점: 경계 / 확신도 중
                         - 근거 1)… 2)…
    us-premarket       1. 월마트(WMT) — 실적 발표됨
                       · 관점: 회피 / 확신도 중상(발표 확정)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from briefing import config

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ── 정규식 ──────────────────────────────────────────────

# 섹션 헤더:  [마감 지수]  /  📈 [지수]  /  🔎 [08:00 장전 관점 점검]
_SECTION = re.compile(r"^\s*(?:[^\w\s\[]{1,3}\s*)?\[([^\]\d][^\]]*)\]\s*(.*)$")

# 종목 블록 시작:  [1] …   /   1. …   /   1)  …
_BLOCK_BRACKET = re.compile(r"^\[(\d{1,2})\]\s+(.+)$")
_BLOCK_DOT = re.compile(r"^(\d{1,2})\.\s+(.+)$")
_BLOCK_PAREN = re.compile(r"^(\d{1,2})\)\s+(.+)$")

# 관점: 주목 / 확신도 중상   ·   관점: 경계(승계) / 확신도 중(발표 확정)
# 관점과 확신도는 함께 오는 것이 원칙이지만 실제로는 표기가 흔들린다.
#   '관점: 경계 / 확신도 중상'   (기본)
#   '관점: 경계 / 확신도: 중상'  (2026-08-17 이후 등장)
#   '관점: 경계'                 (확신도 누락, 2026-08-17 0800)
#   '관점: 경계(승계) / 확신도 중(발표 확정)'
#   '관점 주목 / 확신도 중상'    (콜론 없음, 2026-08-06 1450)
#
# 확신도는 문서상 상·중상·중·하 4단계지만 실제로는 '중하'가 19회(13개 파일) 쓰인다.
# 대안 순서가 중요하다 — '중상|상|중|하' 로 두면 '중하'에서 '중'만 잡혀 조용히 오기록된다.
# 따라서 둘을 독립적으로 찾고 콜론도 선택적으로 둔다. 확신도가 없으면 null 로 두고 경고를 남긴다.
_STANCE_ONLY = re.compile(
    r"관점\s*[:：]?\s*(?P<stance>주목|조건부|경계|회피)\s*(?:\((?P<inherit>[^)]*)\))?"
)
_CONF_ONLY = re.compile(
    r"확신도\s*[:：=]?\s*(?P<conf>중상|중하|상|중|하)\s*(?:\((?P<note>[^)]*)\))?"
)

_SUMMARY = re.compile(r"^한 줄 요약\s*[:：]\s*(.+)$")
_HEADING = re.compile(r"^##\s+(.+)$")
_URL = re.compile(r"https?://[^\s)＞>]+")
_DART_RCP = re.compile(r"rcpNo=(\d{14})")

# 블록 안 필드
_FIELDS = {
    "catalyst": re.compile(r"^\s*[·\-•]?\s*촉매\s*[:：]\s*(.+)$"),
    "reasons": re.compile(r"^\s*[·\-•]?\s*근거\s*[:：]?\s*(.+)$"),
    "invalidation": re.compile(r"^\s*[·\-•]?\s*틀리는 조건\s*[:：]\s*(.+)$"),
    "check_at": re.compile(r"^\s*[·\-•]?\s*확인\s*[:：]\s*(.+)$"),
    "kr_links": re.compile(r"^\s*[·\-•]?\s*한국 연결\s*[:：]\s*(.+)$"),
    "risk": re.compile(r"^\s*[·\-•]?\s*리스크\s*[:：]\s*(.+)$"),
    "sources": re.compile(r"^\s*[·\-•]?\s*출처\s*[:：]\s*(.+)$"),
}

# 근거 나열 표기: ①②③ 또는 1) 2) 3)
_ENUM_CIRCLED = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")
_ENUM_PAREN = re.compile(r"(?<![\d])\d\)")

_CODE = re.compile(r"^\d{6}$")
_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,6}$")


@dataclass
class ParsedBriefing:
    briefing_id: str
    kind: str
    published_at: str
    market: str
    source_url: str
    summary: str | None = None
    heading: str | None = None
    sections: dict[str, str] = field(default_factory=dict)
    views: list[dict] = field(default_factory=list)
    disclosures: list[dict] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "briefing_id": self.briefing_id,
            "kind": self.kind,
            "published_at": self.published_at,
            "market": self.market,
            "source_url": self.source_url,
            "summary": self.summary,
            "heading": self.heading,
            "sections": self.sections,
            "views": self.views,
            "disclosures": self.disclosures,
            "parse_warnings": self.parse_warnings,
        }


# ── 공통 헬퍼 ────────────────────────────────────────────


def parse_stance(text: str) -> dict | None:
    """'관점: 경계(승계) / 확신도 중(발표 확정)' 을 분해한다.

    확신도가 없는 브리핑이 실제로 존재하므로(2026-08-17 0800) 관점만 있어도 성립시킨다.
    없는 값을 지어내지 않고 None 으로 둔다.
    """
    ms = _STANCE_ONLY.search(text)
    if not ms:
        return None
    inherit = (ms.group("inherit") or "").strip()
    mc = _CONF_ONLY.search(text)
    note = (mc.group("note") or "").strip() if mc else ""
    return {
        "stance": ms.group("stance"),
        "stance_inherited": "승계" in inherit,
        "confidence": mc.group("conf") if mc else None,
        "confidence_note": note or None,
    }


def split_reasons(text: str) -> list[str]:
    """'①… ②…' 또는 '1)… 2)…' 를 항목으로 나눈다."""
    t = text.strip()
    if not t:
        return []
    if _ENUM_CIRCLED.search(t):
        parts = _ENUM_CIRCLED.split(t)
    elif len(_ENUM_PAREN.findall(t)) >= 2:
        parts = _ENUM_PAREN.split(t)
    else:
        return [t]
    return [p.strip(" ·-—,") for p in parts if p.strip(" ·-—,")]


def parse_header(raw: str) -> dict:
    """블록 헤더에서 종목명·코드·티커를 뽑는다.

    괄호 안이 6자리 숫자면 한국 코드, 대문자 티커면 미국 종목, 둘 다 아니면 종목명만 남긴다
    (예: '삼성전자 (+8%대)' 의 괄호는 등락률이지 코드가 아니다).
    """
    head = raw.split("—")[0].split("–")[0].strip()
    code = symbol = None
    name = head

    m = re.match(r"^([^()]+?)\s*\(([^)]*)\)", head)
    if m:
        candidate = m.group(2).strip()
        if _CODE.match(candidate):
            name, code = m.group(1).strip(), candidate
        elif _TICKER.match(candidate):
            name, symbol = m.group(1).strip(), candidate
        else:
            name = m.group(1).strip()

    # 남은 가격·등락률 꼬리를 떼어낸다
    name = re.split(r"\s+[+\-]?\d|\s+종가|\s+관점", name)[0].strip(" ·-—")
    return {"name": name or head.strip(), "code": code, "symbol": symbol}


# ── 본 파서 ─────────────────────────────────────────────


def parse(day: str, stem: str, text: str, *, source_url: str = "") -> ParsedBriefing:
    if stem not in config.KINDS:
        raise ValueError(f"알 수 없는 브리핑 종류: {stem}")
    kind, hhmm, market = config.KINDS[stem]

    d = date.fromisoformat(day)
    hh, mm = (int(x) for x in hhmm.split(":"))
    published = datetime.combine(d, time(hh, mm), tzinfo=KST).isoformat()

    out = ParsedBriefing(
        briefing_id=f"{day}-{stem}",
        kind=kind,
        published_at=published,
        market=market,
        source_url=source_url,
    )

    lines = text.splitlines()
    if not lines or not text.strip():
        out.parse_warnings.append("원문이 비어 있다")
        return out

    _extract_sections(lines, out)

    if kind in config.VIEW_BEARING_KINDS:
        out.views = _extract_views(lines, market, out.parse_warnings)
        if not out.views:
            if day < config.STANCE_SYSTEM_START:
                out.parse_warnings.append(
                    f"관점 체계 도입({config.STANCE_SYSTEM_START}) 이전 브리핑 — 관점 없음이 정상. "
                    "본문은 sections에 보존됨"
                )
            else:
                out.parse_warnings.append("종목 관점 블록을 하나도 찾지 못했다 — 형식 변경 의심")

    out.disclosures = _extract_disclosures(out.sections)
    return out


def _extract_sections(lines: list[str], out: ParsedBriefing) -> None:
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current and buf:
            body = "\n".join(buf).strip()
            if body:
                out.sections[current] = (
                    out.sections.get(current, "") + ("\n" if current in out.sections else "") + body
                )

    for ln in lines:
        if m := _HEADING.match(ln):
            out.heading = m.group(1).strip()
            continue
        if m := _SUMMARY.match(ln.strip()):
            out.summary = m.group(1).strip()
            continue
        if m := _SECTION.match(ln):
            flush()
            current = m.group(1).strip()
            buf = [m.group(2)] if m.group(2).strip() else []
            continue
        if current is not None:
            buf.append(ln)
    flush()


def _block_starts(lines: list[str]) -> list[tuple[int, str]]:
    """종목 블록 시작 줄 번호와 헤더 텍스트."""
    starts: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        for pat in (_BLOCK_BRACKET, _BLOCK_DOT, _BLOCK_PAREN):
            if m := pat.match(ln.strip()):
                starts.append((i, m.group(2).strip()))
                break
    return starts


def _extract_views(lines: list[str], market: str, warnings: list[str]) -> list[dict]:
    starts = _block_starts(lines)
    if not starts:
        return []

    views: list[dict] = []
    for idx, (line_no, header) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        # 다음 섹션 헤더가 먼저 나오면 거기서 끊는다
        for j in range(line_no + 1, end):
            m = _SECTION.match(lines[j])
            if m and m.group(1).strip():
                end = j
                break
        block = lines[line_no:end]
        view = _parse_block(header, block, market, warnings)
        if view:
            views.append(view)
    return views


def _parse_block(header: str, block: list[str], market: str, warnings: list[str]) -> dict | None:
    raw = "\n".join(block).strip()
    stance = parse_stance(raw)
    if not stance:
        return None  # 관점이 없으면 종목 관점 블록이 아니다 (복기·캘린더 등)

    ident = parse_header(header)
    view: dict = {
        "name": ident["name"],
        "code": ident["code"],
        "symbol": ident["symbol"],
        "market": market if market in ("KR", "US") else ("US" if ident["symbol"] else "KR"),
        **stance,
        "reasons": [],
        "invalidation": None,
        "check_at": None,
        "kr_links": [],
        "sources": [],
        "raw": raw,
    }

    catalyst_txt: str | None = None
    for ln in block:
        s = ln.strip()
        for key, pat in _FIELDS.items():
            if m := pat.match(s):
                val = m.group(1).strip()
                if key == "catalyst":
                    catalyst_txt = val
                elif key == "reasons":
                    view["reasons"].extend(split_reasons(val))
                elif key == "kr_links":
                    view["kr_links"] = [p.strip() for p in re.split(r"[,·/]", val) if p.strip()]
                elif key == "risk":
                    view["reasons"].extend(split_reasons(val))
                elif key == "sources":
                    view["sources"].extend(_URL.findall(val) or [val])
                elif key == "invalidation":
                    # '틀리는 조건: X / 확인: Y' 가 한 줄에 붙어 오는 변형이 있다
                    head, sep, tail = val.partition("/ 확인:")
                    view["invalidation"] = head.strip()
                    if sep and not view["check_at"]:
                        view["check_at"] = tail.strip()
                else:
                    view[key] = val
                break

    # 1450 변형: '관점: … / 틀리는 조건: … / 확인: …' 이 한 줄에 붙어 온다
    if view["invalidation"] is None and (
        m := re.search(r"틀리는 조건\s*[:：]\s*(.+?)(?:\s*/\s*확인\s*[:：]|$)", raw, re.S)
    ):
        view["invalidation"] = m.group(1).strip()
    if view["check_at"] is None and (m := re.search(r"확인\s*[:：]\s*(.+?)(?:\n|$)", raw)):
        view["check_at"] = m.group(1).strip()

    # 헤더 뒤 설명문을 촉매로 승격 (1450·2130 변형은 촉매 라벨이 없다)
    if catalyst_txt is None and "—" in header:
        tail = header.split("—", 1)[1].strip()
        if tail and not _STANCE_ONLY.search(tail):
            catalyst_txt = tail
    if catalyst_txt:
        view["catalyst"] = {"summary": catalyst_txt}

    view["sources"].extend(u for u in _URL.findall(raw) if u not in view["sources"])

    label = view["code"] or view["symbol"] or view["name"]
    if view["market"] == "KR" and not view["code"]:
        warnings.append(f"{label}: 종목코드 없음 — listing 매핑 필요")
    if not view["confidence"]:
        if "확신도" in raw:
            warnings.append(f"{label}: 확신도 표기를 인식하지 못함 — 정의되지 않은 값 사용 의심")
        else:
            warnings.append(f"{label}: 확신도 없음")
    if len(view["reasons"]) < 2:
        warnings.append(f"{label}: 근거 {len(view['reasons'])}개 (브리핑 규칙은 2개 이상)")
    if not view["invalidation"]:
        warnings.append(f"{label}: 틀리는 조건 없음")
    return view


def _extract_disclosures(sections: dict[str, str]) -> list[dict]:
    """공시 섹션에서 DART 원문 링크를 뽑는다. 공시 본문은 DART API가 정본이다."""
    out: list[dict] = []
    for name, body in sections.items():
        if "공시" not in name:
            continue
        for line in body.splitlines():
            for rcp in _DART_RCP.findall(line):
                out.append({"rcept_no": rcp, "context": line.strip(" ·-"), "section": name})
    return out


def map_codes(views: list[dict], name_to_code: dict[str, str], warnings: list[str]) -> int:
    """종목명 → 6자리 코드 역매핑. 매핑된 개수를 반환한다."""
    n = 0
    for v in views:
        if v.get("market") != "KR" or v.get("code"):
            continue
        code = name_to_code.get(v["name"].replace(" ", ""))
        if code:
            v["code"] = code
            n += 1
            warnings.append(f"{v['name']}: listing 매핑 성공 → {code}")
        else:
            warnings.append(f"{v['name']}: listing 매핑 실패 — code=null 로 보존")
    return n
