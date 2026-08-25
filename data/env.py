"""`.env` 로딩. 어느 계층을 먼저 import 하든 같은 결과가 나오도록 한 곳에 둔다.

예전에는 `decision/config.py` 안에서만 로드했다. 그러면 두 가지가 틀어진다.

1. `data.pipeline` 이나 `briefing.pipeline` 은 `decision` 을 import 하지 않으므로
   `.env` 의 `DART_API_KEY`·`BRIEFING_GITLAB_TOKEN` 을 영영 못 본다.
   README 가 "`.env` 를 만들라"고 하는데 절반만 참인 상태가 된다.
2. `data/config.py` 는 import 시점에 `AIK_DATA_DIR` 을 읽는다. 로딩이 다른 모듈에 있으면
   **import 순서에 따라 DB 경로가 달라진다.** 판단 계층을 먼저 import 한 스크립트와
   `python -m data.pipeline` 이 서로 다른 DB 를 보게 된다.

그래서 이 모듈을 각 계층 config 의 **맨 위**에서 import 한다. 첫 import 때 한 번만 로드된다.
"""

from __future__ import annotations

from pathlib import Path

# 저장소 루트의 .env. 실행 위치와 무관해야 하므로 절대 경로로 잡는다.
ENV_PATH: Path | None = Path(__file__).resolve().parent.parent / ".env"

try:
    from dotenv import load_dotenv

    # override=False: 이미 설정된 환경변수가 우선이다. CI·컨테이너의 주입을 파일이 덮으면 안 된다.
    load_dotenv(ENV_PATH, override=False)
except ImportError:  # pragma: no cover - python-dotenv 는 선언된 의존성이다
    ENV_PATH = None
