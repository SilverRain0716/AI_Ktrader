"""AI 판단 엔진 — 팩을 받아 결정을 만들고, 무엇을 보고 만들었는지를 남긴다.

[ADR 0007](../docs/adr/0007-judgment-engine.md) 의 구현이다. 네 가지가 여기 있다.

1. `derive_arm2()` — 정본 팩에서 브리핑 성분을 **제거**해 Arm 2 입력을 만든다. 순수 함수다.
2. `render_input()` — 팩을 모델에 보낼 문자열로 만든다. **결정론적이어야** 재현 검사가 성립한다.
3. `validate()` — 스키마·계약 위에 **팩을 들고 하는 검사**를 얹는다. 여기서 `constraints` 가
   처음으로 통보에서 강제로 바뀐다.
4. `decide()` — 호출·검증·재시도·기록. 실패도 전부 남긴다.

**도구를 주지 않는다.** 외부 세션이었다면 모델이 팩 밖을 볼 수 있고, 그러면
`Arm 1 − Arm 2` 차이에 브리핑이 아니라 "밖에서 본 것"이 섞여 F3 가 틀린 것을 재게 된다.

Arm 0(정량 랭킹)은 **여기 없다.** ADR 0005 선결 과제 4가 풀리기 전까지 Arm 0 의 랭킹 규칙이
정의되지 않았고(모멘텀 단독이라는 제약만 확인됨), 규칙을 임의로 정하면 그것이 기준선이 된다.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from data import config as dcfg
from decision import contract, providers

log = logging.getLogger("decision")

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"
SCHEMA_PATH = ROOT / "schemas" / "decision.schema.json"

# 렌더러가 바뀌면 프롬프트 파일이 같아도 모델이 받는 입력이 달라진다.
# 결정 행에 남겨 층을 가른다.
RENDER_VERSION = "r1"

# **v2 에서 매매 원칙(ADR 0013)을 실었다.** v1 은 규율(한도·근거 형식·무효화)만 말하고
# "무엇을 근거로 사고 파는가"를 한 줄도 담지 않았다 — 실측(2026-09-01): 프롬프트에
# '파급'·'거래대금'·'재료'·'원칙' 이 각 0회였고, 엔진은 거래대금이 식은 종목을 골랐다.
# **프롬프트 id 는 결정 행에 봉인된다.** F2 는 v1 구간과 v2 구간을 끊어서 재야 한다 —
# 이어 붙이면 "AI 선택"과 "프롬프트 변경"이 섞여 둘 다 해석 불가가 된다.
# v3: 매수 시점의 **재료 소멸** 판정을 넣었다. v2 는 원칙을 실었지만 재료 소멸을
# 청산 조건으로만 다뤘고, 그래서 엔진이 MSCI 리밸런싱 D-Day 로 몰린 일회성 수급을
# 추세로 읽었다(LG이노텍, 거래 5.67배·외국인 7일 연속).
PROMPT_ID = "decision_v3"
API_PARAMS: dict[str, Any] = {
    "max_tokens": 16000,
    "output_config": {"effort": "high"},
    "thinking": {"type": "adaptive"},
}

MAX_ATTEMPTS = 3  # 스키마·계약 위반 재요청 포함

# 사이클별 결정 유효 시각 (KST 기준 시:분). 이 시각을 넘겨 도착한 판단은 집행하지 않는다.
#
# **15:20 은 정규장 접속매매가 끝나는 시각이다.** 15:20~15:30 은 종가 단일가(동시호가)라
# 연속 체결이 없다 — 지정가·조건부 진입 지시가 그 구간에서 의도대로 동작하지 않는다.
# 설계안 v1 4.3 의 실행 창(09:00~15:20)과 4.4 의 스키마 예시가 둘 다 15:20 이다.
CYCLE_VALID_UNTIL = {
    "premarket": (15, 20),
    "midday": (15, 20),
    "preclose": (15, 20),
    # 18:30 은 복기 사이클이다(설계안 4.3) — 주문을 내지 않는다. 그럼에도 결정이 나온다면
    # 다음 거래일 개장 직후까지만 유효하다. **이 값은 설계에 없는 임시값이다.**
    "postmarket": (9, 5),
    "event": (15, 20),
}


class DecisionRefused(RuntimeError):
    """판단을 만들 수 없는 상태. 팩이 아니라 판단 쪽 문제다."""


# ── 스키마 ──────────────────────────────────────────────


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema())


def prompt_text(prompt_id: str = PROMPT_ID) -> str:
    return (PROMPT_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")


# ── Arm 2 파생 ──────────────────────────────────────────


def derive_arm2(pack: dict) -> dict:
    """브리핑 성분을 제거한 입력. **팩을 두 번 빌드하지 않는다.**

    두 번 빌드하면 그 사이에 DB 가 변할 수 있고, 그 순간 "같은 팩에 대한 대응비교"가
    문서상으로만 남는다(ADR 0005 3-arm).

    종목을 **가리지 않고 제거**하는 이유: F3 의 반증 시 행동이 정확히 "브리핑 채널을
    유니버스에서 제거"다. 출처만 가리면 "이 종목이 시야에 있다"는 사실 자체가
    브리핑 유래 정보라 F3 에 새는 경로가 남는다.
    """
    out = copy.deepcopy(pack)
    out["briefings"] = []

    universe = []
    for item in out.get("universe", []):
        channels = [c for c in item.get("channels", []) if c != "briefing"]
        if not channels:
            continue  # briefing 채널로만 들어온 종목 — 시야에서 없앤다
        item["channels"] = channels
        item["screen_reasons"] = [
            r for r in item.get("screen_reasons", []) if not r.startswith("briefing:")
        ]
        universe.append(item)
    out["universe"] = universe

    dq = out.setdefault("data_quality", {})
    dq["warnings"] = [w for w in dq.get("warnings", []) if "브리핑" not in w]
    dq.pop("missing_briefings", None)

    return out


def pack_for_arm(pack: dict, arm: int) -> dict:
    if arm == 1:
        return pack
    if arm == 2:
        return derive_arm2(pack)
    raise DecisionRefused(f"arm={arm} 은 LLM 판단 대상이 아니다 (1 또는 2)")


# ── 렌더링 ──────────────────────────────────────────────


def render_input(pack: dict, arm: int) -> str:
    """모델에 보낼 입력. **결정론적이어야 한다** — `render_input(pack) == 저장된 바이트`가
    재현 검사이자 회귀 테스트이기 때문이다(ADR 0007 근거 5).

    그래서 `sort_keys=True` 다. 딕셔너리 순서가 파이썬 버전이나 삽입 순서에 흔들리면
    같은 팩이 다른 입력을 만들고, 그때 재현 검사는 통과하지 않는 것이 아니라 **의미를 잃는다.**
    """
    body = pack_for_arm(pack, arm)
    return json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1)


# ── 팩을 들고 하는 검사 ─────────────────────────────────


def validate(payload: dict, pack_input: dict, arm: int) -> list[str]:
    """스키마·계약을 통과한 출력에 대해 **팩과 대조**한다.

    `contract.check_payload()` 는 팩 없이 판정할 수 있는 것만 본다. 여기는 그 위층이다 —
    ADR 0006 이 말한 1차 엣지(규율)가 실제 강제로 바뀌는 지점이고,
    `03-current-state.md` 3.2 의 "constraints 는 통보될 뿐 강제되지 않는다"를 되돌린다.
    """
    problems: list[str] = []

    universe = {u["code"]: u for u in pack_input.get("universe", [])}
    held = {p["code"]: p for p in pack_input.get("positions", [])}
    con = pack_input.get("constraints", {})
    blocked = set(con.get("blocked_codes") or [])
    briefing_ids = {b["briefing_id"] for b in pack_input.get("briefings", []) if "briefing_id" in b}

    entries = [d for d in payload.get("decisions", []) if d["action"] in ("BUY", "ADD")]

    # ── 종목 실재·소속 ──
    for d in payload.get("decisions", []):
        code, action = d["code"], d["action"]
        if action == "BUY":
            if code not in universe:
                problems.append(f"{code}: BUY 인데 이 arm 의 유니버스에 없다")
            if code in held:
                problems.append(f"{code}: BUY 인데 이미 보유 중이다 (ADD 여야 한다)")
            if code in blocked:
                problems.append(f"{code}: 오늘 손절한 종목이다 (blocked_codes)")
        elif action in ("ADD", "HOLD", "TRIM", "EXIT"):
            if code not in held:
                problems.append(f"{code}: {action} 인데 보유 종목이 아니다")

        # 참조 무결성 — Arm 2 에서 비어 있지 않으면 파생 누수다
        for ref in d.get("briefing_refs") or []:
            if ref not in briefing_ids:
                problems.append(f"{code}: briefing_refs 의 {ref} 가 입력에 없다")
        if arm == 2 and d.get("briefing_refs"):
            problems.append(f"{code}: **Arm 2 인데 briefing_refs 가 있다 — 파생 누수다**")

    # ── 산술 재검증 (constraints 강제) ──
    if con:
        if con.get("daily_loss_limit_hit") and entries:
            problems.append(f"일일 손실 한도에 걸렸는데 신규 진입 {len(entries)}건이 있다")

        cap = con.get("max_new_entries_this_cycle")
        if cap is not None and len(entries) > cap:
            problems.append(f"신규 진입 {len(entries)}건 > 한도 {cap}건")

        per_name = con.get("max_weight_pct_per_name")
        for d in entries:
            w = d.get("weight_pct")
            if per_name is not None and w is not None and w > per_name:
                problems.append(f"{d['code']}: 비중 {w}% > 종목 한도 {per_name}%")

        exits = {d["code"] for d in payload.get("decisions", []) if d["action"] == "EXIT"}
        after = (len(held) - len(exits)) + len([d for d in entries if d["action"] == "BUY"])
        cap = con.get("max_positions")
        if cap is not None and after > cap:
            problems.append(f"결과 보유 {after}종목 > 상한 {cap}종목")

        problems += _sector_problems(entries, held, universe, con)
        problems += _liquidity_problems(entries, universe, pack_input, con)

    # ── 물리 검사 ──
    for d in entries:
        u = universe.get(d["code"])
        close = (u or {}).get("indicators", {}).get("close")
        entry = d.get("entry") or {}
        price = entry.get("price")
        if close and price and abs(price - close) / close > 0.10:
            problems.append(
                f"{d['code']}: 지정가 {price:,} 가 종가 {close:,} 대비 10% 넘게 벌어졌다"
            )
        stop = d.get("stop") or {}
        if close and stop.get("type") == "PRICE" and stop.get("value", 0) >= close:
            problems.append(f"{d['code']}: 손절가 {stop['value']:,} 가 종가 {close:,} 이상이다")
        target = d.get("target") or {}
        if close and target.get("type") == "PRICE" and target.get("value", 0) <= close:
            problems.append(f"{d['code']}: 목표가 {target['value']:,} 가 종가 {close:,} 이하다")

    return problems


def _sector_problems(entries, held, universe, con) -> list[str]:
    cap = con.get("max_weight_pct_per_sector")
    if cap is None:
        return []
    by_sector: dict[str, float] = {}
    for p in held.values():
        sector = (universe.get(p["code"], {}) or {}).get("sector") or p.get("sector") or "기타"
        by_sector[sector] = by_sector.get(sector, 0.0) + (p.get("weight_pct") or 0.0)
    for d in entries:
        sector = (universe.get(d["code"], {}) or {}).get("sector") or "기타"
        by_sector[sector] = by_sector.get(sector, 0.0) + (d.get("weight_pct") or 0.0)
    return [f"섹터 '{s}' 합계 {w:.1f}% > 한도 {cap}%" for s, w in by_sector.items() if w > cap]


def _liquidity_problems(entries, universe, pack_input, con) -> list[str]:
    """주문금액이 유동성 대비 과한가. 단위 주의 — adv20 은 **억원**이다."""
    cap = con.get("max_order_vs_adv_pct")
    equity = (pack_input.get("account") or {}).get("total_equity_krw")
    if cap is None or not equity:
        return []
    out = []
    for d in entries:
        w = d.get("weight_pct")
        adv_eok = (universe.get(d["code"], {}) or {}).get("indicators", {}).get("adv20_eok_krw")
        if w is None or not adv_eok:
            continue
        order_krw = equity * w / 100.0
        adv_krw = adv_eok * 1e8  # 억원 → 원
        pct = order_krw / adv_krw * 100.0
        if pct > cap:
            out.append(f"{d['code']}: 주문금액이 20일 평균거래대금의 {pct:.1f}% > 한도 {cap}%")
    return out


# ── 저장 ────────────────────────────────────────────────


def _valid_until(cycle: str, now: datetime) -> str:
    hh, mm = CYCLE_VALID_UNTIL.get(cycle, (15, 20))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end.isoformat()


def save_decision(conn: sqlite3.Connection, row: dict) -> None:
    """append-only. UPDATE 경로를 만들지 않는다 (ADR 0007 결정 3)."""
    cols = [
        "decision_id",
        "attempt",
        "pack_id",
        "pack_sha256",
        "arm",
        "cycle",
        "generated_at",
        "valid_until",
        "provider",
        "model",
        "prompt_id",
        "prompt_sha256",
        "render_version",
        "api_params",
        "rendered_input",
        "raw_response",
        "payload",
        "status",
        "problems",
        "monitorable",
        "unmonitorable",
        "request_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
    ]
    conn.execute(
        f"INSERT INTO decisions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(row.get(c) for c in cols),
    )


def _monitorability(payload: dict | None) -> tuple[int | None, int | None]:
    if not payload:
        return None, None
    invs = [d.get("invalidation") or {} for d in payload.get("decisions", [])]
    ok = sum(1 for i in invs if contract.is_monitorable(i))
    return ok, len(invs) - ok


# ── 호출 ────────────────────────────────────────────────


def _provider(name: str | None = None, *, client=None):
    """제공자를 만든다. 자격증명이 없으면 여기서 분명하게 멈춘다."""
    try:
        return providers.get(name, client=client)
    except providers.MissingCredential as e:
        raise DecisionRefused(f"{e} 쓰려는 제공자의 키만 있으면 된다 — .env 를 보라.") from e


def call_model(provider, model: str, rendered: str, prompt: str, schema: dict):
    """도구 없는 단일 호출. 출력은 스키마로 강제한다.

    강제하고도 아래에서 다시 검증한다 — 강제됐다는 말과 강제됐는지 확인하는 것은 다르다.
    제공자마다 강제 범위가 달라서(OpenAI strict 는 `pattern`·`minimum` 을 보지 않는다)
    **정확성을 디코더에 맡기지 않는 것**이 제공자 교체를 안전하게 만든다.
    """
    return provider.call(
        model=model, system=prompt, user=rendered, schema=schema, params=API_PARAMS
    )


def decide(
    conn: sqlite3.Connection,
    pack: dict,
    arm: int,
    *,
    provider=None,
    model: str | None = None,
    client=None,
    now: datetime | None = None,
) -> dict:
    """한 arm 의 판단을 만들고 기록한다. **모든 시도가 남는다.**

    재시도는 같은 `decision_id` 를 재사용한다 — 그래야 "같은 id 의 주문은 두 번 나가지
    않는다"가 성립한다(ADR 0007 근거 4).
    """
    now = now or datetime.now(dcfg.KST)
    provider = provider or _provider(client=client)
    model = providers.resolve_model(provider, model)
    pack_input = pack_for_arm(pack, arm)
    rendered = render_input(pack, arm)
    prompt = prompt_text()
    schema = _schema()
    validator = Draft202012Validator(schema)

    base = {
        "decision_id": contract.decision_id(pack["pack_id"], arm),
        "pack_id": pack["pack_id"],
        "pack_sha256": contract.canonical_sha256(pack),
        "arm": arm,
        "cycle": pack["cycle"],
        "generated_at": now.isoformat(),
        "valid_until": _valid_until(pack["cycle"], now),
        "provider": provider.name,
        "model": model,
        "prompt_id": PROMPT_ID,
        "prompt_sha256": contract.canonical_sha256(prompt),
        "render_version": RENDER_VERSION,
        "api_params": json.dumps(API_PARAMS, ensure_ascii=False, sort_keys=True),
        "rendered_input": rendered,
    }
    last: dict = {}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        row = {**base, "attempt": attempt}
        try:
            got = call_model(provider, model, rendered, prompt, schema)
        except (DecisionRefused, providers.MissingCredential):
            # 설정 문제는 재시도할 것이 아니다. 3번 실패로 기록하면 원인이 가려진다.
            raise
        except Exception as e:  # API 장애·타임아웃 — 판단의 부재이지 abstain 이 아니다
            row.update(status="api_error", problems=json.dumps([f"{type(e).__name__}: {e}"]))
            save_decision(conn, row)
            last = row
            log.warning("arm %d 시도 %d — API 실패: %s", arm, attempt, e)
            continue

        row.update(
            raw_response=got.raw,
            request_id=got.request_id,
            input_tokens=got.input_tokens,
            output_tokens=got.output_tokens,
            latency_ms=got.latency_ms,
        )

        # 잘린 응답을 부분 파싱하지 않는다 — 우연히 유효한 JSON 일 수 있다
        if got.stop_reason != providers.END_TURN:
            row.update(
                status="api_error" if got.stop_reason == providers.REFUSAL else "schema_rejected",
                problems=json.dumps([f"stop_reason={got.stop_reason} — 전체 거부"]),
            )
            save_decision(conn, row)
            last = row
            continue

        try:
            payload = json.loads(got.raw)
        except json.JSONDecodeError as e:
            row.update(status="schema_rejected", problems=json.dumps([f"JSON 파싱 실패: {e}"]))
            save_decision(conn, row)
            last = row
            continue

        errors = [
            f"{'/'.join(map(str, e.path))}: {e.message}"
            for e in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        ]
        if errors:
            row.update(
                status="schema_rejected", problems=json.dumps(errors[:10], ensure_ascii=False)
            )
            save_decision(conn, row)
            last = row
            continue

        problems = contract.check_payload(payload) + validate(payload, pack_input, arm)
        mon, unmon = _monitorability(payload)
        row.update(monitorable=mon, unmonitorable=unmon)
        if problems:
            row.update(
                status="contract_rejected",
                problems=json.dumps(problems[:10], ensure_ascii=False),
            )
            save_decision(conn, row)
            last = row
            log.warning("arm %d 시도 %d — 계약 위반 %d건", arm, attempt, len(problems))
            continue

        # 러너가 봉인한다 — 모델이 만든 값이 아니다
        payload["name"] = None  # code 가 진실이다. 이름은 실행 계층이 채운다
        row.update(
            payload=json.dumps(payload, ensure_ascii=False),
            status="abstain" if payload.get("abstain") else "ok",
        )
        save_decision(conn, row)
        return row

    raise DecisionRefused(
        f"arm {arm}: {MAX_ATTEMPTS}회 시도가 모두 실패했다 (마지막 status={last.get('status')}). "
        "모든 시도가 decisions 테이블에 남아 있다."
    )


def decide_pair(
    conn: sqlite3.Connection,
    pack: dict,
    *,
    provider=None,
    model: str | None = None,
    client=None,
    **kw,
) -> dict:
    """Arm 1·2 를 같은 팩에 대해 짝으로 낸다.

    한쪽만 실패하면 성공한 쪽은 그대로 쓰되 **쌍에서 제외** 표시를 돌려준다 —
    짝 없는 관측을 쌍으로 세면 McNemar 가 오염된다(ADR 0007).

    **클라이언트는 루프 밖에서 한 번 만든다.** 안에서 만들면 API 키가 없을 때
    "설정이 없어서 아무것도 못 했다"가 "양쪽 arm 이 실패했다"로 보고되고,
    사람은 판단이 나빴다고 읽는다 — 원인과 증상이 뒤바뀐다.
    """
    # 제공자·모델을 한 번 정해 **두 arm 에 같은 것을 쓴다.** arm 마다 다시 고르면
    # 환경변수가 중간에 바뀌거나 폴백이 끼어들 때 `Arm 1 − Arm 2` 차이에 제공자 차이가
    # 섞이고, 그러면 F3 는 브리핑이 아니라 모델 차이를 잰다.
    provider = provider or _provider(client=client)  # 설정 문제는 여기서 그대로 올라간다
    model = providers.resolve_model(provider, model)

    out: dict[str, Any] = {"paired": True, "provider": provider.name, "model": model}
    for arm in (1, 2):
        try:
            out[f"arm{arm}"] = decide(conn, pack, arm, provider=provider, model=model, **kw)
        except DecisionRefused as e:
            log.error("arm %d 실패: %s", arm, e)
            out[f"arm{arm}"] = None
            out["paired"] = False
    return out


__all__ = [
    "PROMPT_ID",
    "RENDER_VERSION",
    "DecisionRefused",
    "call_model",
    "decide",
    "decide_pair",
    "derive_arm2",
    "pack_for_arm",
    "render_input",
    "save_decision",
    "validate",
]
