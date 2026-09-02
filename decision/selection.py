"""순위대로 봉투에 담는다. **자르는 곳은 여기 하나뿐이다.**

## 왜 AI 에게 개수를 주지 않는가

`max_new_entries_this_cycle` 을 팩에 실어 AI 에게 "2건까지"라고 말하면, 그것은 상한이
아니라 **채워야 하는 칸**으로 읽힌다. 실제로 2026-09-01 프리마켓은 두 arm 모두 정확히
2건을 냈다 — 세 번째가 없어서인지 낼 수 없어서인지 기록에 남지 않는다.

ADR 0009 가 나눈 대로 되돌린다. **고르는 것은 AI(선택), 자르는 것은 기계(봉투).**
AI 는 살 만한 것을 전부 순위와 함께 내고, 담기지 않은 후보는 버리지 않고
사유와 함께 남긴다 — 나중에 *"3위를 잘랐는데 그게 더 올랐나"* 를 셀 수 있어야 한다.

## 엔진과 게이트가 같은 함수를 쓴다

각자 자르면 둘이 갈라진다. 엔진이 검증한 조합과 게이트가 주문하는 조합이 다르면
어느 쪽도 근거가 되지 못한다.
"""

from __future__ import annotations

from dataclasses import dataclass

NEW_ACTIONS = ("BUY", "ADD")


@dataclass(frozen=True)
class Selection:
    taken: tuple[dict, ...]
    deferred: tuple[tuple[dict, str], ...]  # (결정, 담기지 않은 사유)

    @property
    def taken_codes(self) -> set[str]:
        return {d["code"] for d in self.taken}


def rank_of(d: dict) -> int:
    """순위. 없으면 **맨 뒤**로 둔다 — 없는 것을 1위로 올리면 순위를 안 매기는 쪽이 유리해진다."""
    r = d.get("rank")
    return r if isinstance(r, int) and r >= 1 else 10**6


def order_problems(entries: list[dict]) -> list[str]:
    """순위 자체의 흠. 중복·결측은 **자르는 순서를 결정할 수 없게** 만든다."""
    problems: list[str] = []
    seen: dict[int, str] = {}
    for d in entries:
        r = d.get("rank")
        if not isinstance(r, int) or r < 1:
            problems.append(
                f"{d['code']}: 신규 진입인데 rank 가 없다 — 우선순위 없이는 자를 수 없다"
            )
            continue
        if r in seen:
            problems.append(f"rank {r} 이 {seen[r]} 과 {d['code']} 에 겹친다")
        seen[r] = d["code"]
    return problems


def select(
    entries: list[dict],
    *,
    constraints: dict,
    held: dict | None = None,
    exits: set[str] | None = None,
    universe: dict | None = None,
) -> Selection:
    """순위 오름차순으로 담되, 봉투에 걸리면 그 후보만 미루고 다음으로 넘어간다.

    **걸린 데서 멈추지 않는다.** 3위가 섹터 한도에 걸려도 4위는 다른 섹터일 수 있다.
    """
    held = held or {}
    exits = exits or set()
    universe = universe or {}

    max_new = constraints.get("max_new_entries_this_cycle")
    max_pos = constraints.get("max_positions")
    sector_cap = constraints.get("max_weight_pct_per_sector")

    def sector_of(code: str, fallback: dict | None = None) -> str:
        return (
            (universe.get(code, {}) or {}).get("sector") or (fallback or {}).get("sector") or "기타"
        )

    by_sector: dict[str, float] = {}
    for p in held.values():
        if p["code"] in exits:
            continue
        s = sector_of(p["code"], p)
        by_sector[s] = by_sector.get(s, 0.0) + (p.get("weight_pct") or 0.0)

    positions = len([c for c in held if c not in exits])
    taken: list[dict] = []
    deferred: list[tuple[dict, str]] = []

    for d in sorted(entries, key=rank_of):
        r = rank_of(d)
        label = f"{r}위" if r < 10**6 else "순위없음"
        if max_new is not None and len(taken) >= max_new:
            deferred.append((d, f"{label} — 이번 사이클 신규 진입 한도 {max_new}건을 이미 채웠다"))
            continue
        # ADD 는 이미 보유 중이라 종목 수를 늘리지 않는다
        grows = d.get("action") == "BUY"
        if max_pos is not None and grows and positions >= max_pos:
            deferred.append((d, f"{label} — 보유 종목 상한 {max_pos}종목에 이미 닿았다"))
            continue
        s = sector_of(d["code"])
        w = d.get("weight_pct") or 0.0
        if sector_cap is not None and by_sector.get(s, 0.0) + w > sector_cap:
            deferred.append((d, f"{label} — 섹터 '{s}' 합계가 한도 {sector_cap}% 를 넘는다"))
            continue
        taken.append(d)
        by_sector[s] = by_sector.get(s, 0.0) + w
        if grows:
            positions += 1

    return Selection(tuple(taken), tuple(deferred))


__all__ = ["NEW_ACTIONS", "Selection", "order_problems", "rank_of", "select"]
