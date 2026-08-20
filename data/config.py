"""데이터 계층 공용 설정.

경로와 상수는 전부 여기서만 정의한다. 모듈마다 흩어지면 나중에 손댈 곳이 늘어난다.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# ── 경로 ────────────────────────────────────────────────
# 저장소 루트 = 이 파일의 부모의 부모
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AIK_DATA_DIR", REPO_ROOT / "data" / "warehouse"))
DB_PATH = DATA_DIR / "market.db"

# ── 데이터 소스 정책 ─────────────────────────────────────
# 수정주가 소스와 원본가 소스를 섞으면 액면분할 종목에서 수익률이 50배 튄다.
# 일봉의 정본(canonical)은 하나만 둔다.
CANONICAL_OHLCV_SOURCE = "naver"  # 수정주가 반영

# 네이버는 비공식 API다. 차단당하지 않도록 간격을 둔다.
NAVER_REQUEST_INTERVAL_SEC = 0.35
NAVER_TIMEOUT_SEC = 20
NAVER_MAX_RETRY = 3

# DART OpenAPI. 인증키당 1일 20,000건이라 여유롭지만, 예의상 간격을 둔다.
DART_REQUEST_INTERVAL_SEC = 0.2
DART_TIMEOUT_SEC = 20
DART_MAX_RETRY = 3
DART_PAGE_COUNT = 100  # 페이지당 최대치
DART_MAX_PAGES = 30  # 하루 3,000건이면 충분하다. 초과 시 경고를 남긴다.

# 일봉 최초 적재 시작일. 지표 계산에 필요한 최소 구간보다 넉넉히 잡는다.
DEFAULT_HISTORY_START = date(2015, 1, 1)

# ── 지표 파라미터 ────────────────────────────────────────
MA_PERIODS = (5, 20, 60, 120)
RSI_PERIOD = 14
ATR_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RS_PERIOD = 20  # 상대강도 기준 기간
ADV_PERIOD = 20  # 평균 거래대금 기간

# 지표를 계산하려면 최소 이만큼의 유효 봉이 필요하다. 미달이면 None으로 둔다.
MIN_BARS_FOR_INDICATORS = 120

# ── 벤치마크 지수 (상대강도 계산용) ──────────────────────
# 네이버 siseJson은 지수도 같은 형식으로 준다.
INDEX_SYMBOLS = {
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
}


def today_kst() -> date:
    return datetime.now(KST).date()


def default_start_for(full: bool = False) -> date:
    """증분 갱신 시작일. full이면 전체 재적재."""
    return DEFAULT_HISTORY_START if full else today_kst() - timedelta(days=400)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
