"""브리핑 계층 설정."""

from __future__ import annotations

import os

# ── GitLab 저장소 ────────────────────────────────────────
GITLAB_HOST = os.getenv("BRIEFING_GITLAB_HOST", "https://gitlab.com")
GITLAB_PROJECT = os.getenv("BRIEFING_GITLAB_PROJECT", "SilverRain0716/briefings")
GITLAB_BRANCH = os.getenv("BRIEFING_GITLAB_BRANCH", "main")
GITLAB_ROOT = os.getenv("BRIEFING_GITLAB_ROOT", "briefings")

REQUEST_TIMEOUT_SEC = 25
MAX_RETRY = 3
REQUEST_INTERVAL_SEC = 0.15

# ── 브리핑 종류 ──────────────────────────────────────────
# 키 = 파일명 stem, 값 = (정규화 kind, 발행시각 HH:MM KST, 시장)
#
# 브리핑 스케줄은 2026-08-05에 개편됐다. 그 이전 스케줄(0700/0900/1400/1800 경제,
# 1510 장중리더, 1600 마감, 1700 마감심층)도 GitLab에 남아 있으므로 전부 인식한다.
# 인식하지 못하는 stem이 나오면 파싱을 건너뛰는 게 아니라 오류로 드러나야 한다.
KINDS: dict[str, tuple[str, str, str]] = {
    # 현행 스케줄 (2026-08-05~)
    "0500-us-close": ("us-close", "05:00", "US"),
    "0800-kr-premarket-deep": ("kr-premarket-deep", "08:00", "KR"),
    "1200-daily-economy": ("daily-economy", "12:00", "GLOBAL"),
    "1450-kr-preclose": ("kr-preclose", "14:50", "KR"),
    "1800-kr-close-deep": ("kr-close-deep", "18:00", "KR"),
    "2130-us-premarket": ("us-premarket", "21:30", "US"),
    # 구 스케줄 (~2026-08-04)
    "0700-daily-economy": ("daily-economy", "07:00", "GLOBAL"),
    "0900-daily-economy": ("daily-economy", "09:00", "GLOBAL"),
    "1400-daily-economy": ("daily-economy", "14:00", "GLOBAL"),
    "1800-daily-economy": ("daily-economy", "18:00", "GLOBAL"),
    "1510-kr-intraday-leaders": ("kr-intraday-leaders", "15:10", "KR"),
    "1600-kr-close": ("kr-close", "16:00", "KR"),
    "1700-kr-close-deep": ("kr-close-deep", "17:00", "KR"),
}

# 관점 체계(주목·조건부·경계·회피 + 확신도 + 틀리는 조건)가 도입된 날.
# 실측: 188개 브리핑 전수 조사 결과 2026-08-04까지는 관점이 0건, 08-05부터 등장한다.
# 이 날짜 이전 브리핑에 관점이 없는 것은 정상이며 파싱 실패가 아니다.
STANCE_SYSTEM_START = "2026-08-05"

# 관점·확신도 허용값. schemas/briefing.schema.json 의 enum과 일치해야 한다.
STANCES = ("주목", "조건부", "경계", "회피")
# 문서상 4단계지만 실측 결과 '중하'가 13개 파일 19회 쓰였다. 현실을 반영해 5단계로 둔다.
# 정규식 대안 순서 주의: 긴 것을 먼저 둬야 '중하'가 '중'으로 잘리지 않는다.
CONFIDENCES = ("상", "중상", "중", "중하", "하")

# 종목 관점을 담는 브리핑. 나머지는 시장 배경만 추출한다.
VIEW_BEARING_KINDS = frozenset(
    {"us-close", "kr-premarket-deep", "kr-preclose", "kr-close-deep", "us-premarket"}
)

# 종목 관점을 담지 않는 종류. 경제 브리핑과 구버전 요약은 시장 배경만 제공한다.
BACKGROUND_ONLY_KINDS = frozenset({"daily-economy", "kr-intraday-leaders", "kr-close"})
