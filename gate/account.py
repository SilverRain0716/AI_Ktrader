"""증권사 계좌 잔고 조회. **주문이 아니라 확인이다.**

페이퍼 시드(`AIK_PAPER_EQUITY_KRW`)와 실제 계좌 예수금이 다르면 두 가지가 어긋난다.

1. **시드가 예수금보다 크면 주문이 거부되거나 미수가 남는다.** 이건 막아야 한다
2. 시드가 예수금보다 작으면 주문은 나가지만 **성적 해석이 어긋난다** — 5억 계좌에서
   2천만어치만 굴린 결과를 5억 수익률로 읽으면 안 된다. 이건 막지 않고 드러낸다

실측(2026-09-01): 키움 모의계좌 예수금이 **5억**인데 페이퍼 시드는 그보다 작았다.
"""

from __future__ import annotations

import logging

log = logging.getLogger("gate.account")

DEPOSIT_TR = "kt00001"  # 예수금상세현황요청
DEPOSIT_PATH = "/api/dostk/acnt"


def deposit_krw(client) -> int | None:
    """주문 가능 예수금(원). **못 받으면 None 이지 0 이 아니다.**

    0 으로 접으면 "예수금이 없다"가 되어 모든 주문이 시드 초과로 막힌다 —
    확인 실패와 잔고 부족이 같아진다.
    """
    j = client.post(DEPOSIT_TR, DEPOSIT_PATH, {"qry_tp": "3"})
    raw = str(j.get("entr") or "").strip()
    if not raw.lstrip("-").isdigit():
        log.warning("예수금 필드를 읽을 수 없다: entr=%r", j.get("entr"))
        return None
    return int(raw)


def check_seed(equity_krw: int, deposit: int | None) -> tuple[list[str], list[str]]:
    """`(차단 사유, 알림)`. **차단과 알림을 나눈다.**

    막을 것만 막고 나머지는 드러낸다 — 둘을 섞으면 못 막을 것을 막거나
    막아야 할 것을 흘린다.
    """
    if deposit is None:
        return (["계좌 예수금을 확인하지 못했다 — 시드가 잔고를 넘는지 알 수 없다"], [])
    if equity_krw > deposit:
        return (
            [
                f"페이퍼 시드({equity_krw:,}원)가 계좌 예수금({deposit:,}원)보다 크다. "
                "주문이 거부되거나 미수가 남는다"
            ],
            [],
        )
    if equity_krw != deposit:
        return (
            [],
            [
                f"시드({equity_krw:,}원)와 예수금({deposit:,}원)이 다르다. "
                "주문은 시드 기준으로 나가므로 계좌 수익률로 성적을 읽으면 어긋난다"
            ],
        )
    return ([], [])


__all__ = ["DEPOSIT_TR", "check_seed", "deposit_krw"]
