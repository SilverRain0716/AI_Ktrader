"""측정 표 — **구간마다 표본 수를 찍는가.**

한 번 크게 당했다(2026-09-08): 표본 수를 `+5일` 기준으로만 찍었더니 briefing 의
`+20일` 이 **14건·종목 9개**인 것을 못 봤다. 그 숫자로 "briefing 이 모집단보다
+6.4%p 낫다"는 결론을 냈고, lookback 을 1→40 으로 바꿔도 값이 1.45% 로 고정된
신호까지 놓쳤다 — **표본이 안 늘고 있었던 것이다.**

여기서 지키는 것은 표의 모양이 아니라 **얇은 칸이 얇아 보이는가**다.
"""

from __future__ import annotations

from scripts.measure_channel_fit import HORIZONS, fwd_header, fwd_row


def test_구간마다_제_표본_수를_찍는다():
    r = {5: [1.0] * 100, 10: [1.0] * 50, 20: [1.0] * 14}
    out = fwd_row("t", r)
    for n in ("100", "50", "14"):
        assert n in out, f"표본 {n} 이 안 보인다: {out}"


def test_얇은_칸을_두꺼운_칸의_수로_가리지_않는다():
    """**하나의 n 으로는 표가 만들어지지 않는다** — 구간마다 필요한 봉 수가 다르다."""
    r = {5: [1.0] * 1000, 10: [1.0] * 1000, 20: [1.0] * 3}
    out = fwd_row("t", r)
    assert out.count("1,000") == 2
    assert "3" in out.split()[-3:][0] or " 3 " in out or out.rstrip().endswith("%")
    # 20일 칸이 1,000 으로 찍히면 안 된다
    assert out.count("1,000") != 3


def test_없는_구간은_숫자를_만들어내지_않는다():
    out = fwd_row("t", {5: [1.0] * 10})
    assert "-" in out


def test_기준선_대비_차이는_같은_구간끼리_뺀다():
    """+5일 값을 +20일 기준선에서 빼면 차이가 통째로 틀린다."""
    r = {5: [10.0], 10: [10.0], 20: [10.0]}
    base = {5: [0.0], 10: [5.0], 20: [8.0]}
    out = fwd_row("t", r, base=base)
    assert "+10.0p" in out and "+5.0p" in out and "+2.0p" in out


def test_머리글과_칸_수가_맞는다():
    assert fwd_header().count("표본") == len(HORIZONS)
