"""GitLab에서 브리핑 원문을 가져온다.

인증 토큰은 환경변수로만 받는다. 코드에 박지 않는다 — 이 저장소는 public이다.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from briefing import config

log = logging.getLogger(__name__)

_last_call_at = 0.0


class GitLabError(RuntimeError):
    """조회 실패. 빈 결과로 삼키지 않는다."""


class GitLabTokenMissing(GitLabError):
    """토큰이 없을 때. 배치 전체를 죽이지 않고 이 태스크만 건너뛰게 한다."""


@dataclass(frozen=True)
class BriefingFile:
    day: str  # YYYY-MM-DD
    stem: str  # 0800-kr-premarket-deep
    text: str


def _token() -> str:
    tok = os.getenv("BRIEFING_GITLAB_TOKEN", "").strip()
    if not tok:
        raise GitLabTokenMissing("BRIEFING_GITLAB_TOKEN 이 설정되지 않았다. .env 를 확인하라.")
    return tok


def _api_base() -> str:
    proj = urllib.parse.quote(config.GITLAB_PROJECT, safe="")
    return f"{config.GITLAB_HOST}/api/v4/projects/{proj}/repository"


def _throttle() -> None:
    global _last_call_at
    wait = config.REQUEST_INTERVAL_SEC - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _get(url: str, params: dict | None = None) -> httpx.Response:
    headers = {"PRIVATE-TOKEN": _token()}
    last: Exception | None = None
    for attempt in range(1, config.MAX_RETRY + 1):
        _throttle()
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=config.REQUEST_TIMEOUT_SEC)
            if r.status_code == 404:
                return r  # 호출부가 판단한다
            if r.status_code == 401:
                raise GitLabError("GitLab 인증 실패 — 토큰이 만료됐거나 권한이 없다")
            r.raise_for_status()
            return r
        except GitLabError:
            raise
        except Exception as e:
            last = e
            log.warning("GitLab 요청 실패 (%d/%d): %s", attempt, config.MAX_RETRY, e)
            time.sleep(1.0 * attempt)
    raise GitLabError(f"{url} 요청이 {config.MAX_RETRY}회 모두 실패") from last


def list_days() -> list[str]:
    """브리핑이 존재하는 날짜 목록 (YYYY-MM-DD), 오름차순."""
    out: list[str] = []
    page = 1
    while True:
        r = _get(
            f"{_api_base()}/tree",
            {"path": config.GITLAB_ROOT, "per_page": 100, "page": page},
        )
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        out.extend(x["name"] for x in items if x.get("type") == "tree")
        if len(items) < 100:
            break
        page += 1
    return sorted(out)


def list_files(day: str) -> list[str]:
    """해당 날짜의 브리핑 파일 stem 목록."""
    r = _get(f"{_api_base()}/tree", {"path": f"{config.GITLAB_ROOT}/{day}", "per_page": 100})
    if r.status_code == 404:
        return []
    items = r.json()
    if not isinstance(items, list):
        return []
    return sorted(x["name"][:-3] for x in items if x["name"].endswith(".md"))


def fetch(day: str, stem: str) -> BriefingFile | None:
    """브리핑 원문. 파일이 없으면 None (오류가 아니다)."""
    path = urllib.parse.quote(f"{config.GITLAB_ROOT}/{day}/{stem}.md", safe="")
    r = _get(f"{_api_base()}/files/{path}/raw", {"ref": config.GITLAB_BRANCH})
    if r.status_code == 404:
        return None
    text = r.text
    # GitLab이 404 JSON을 200으로 흘려보내는 경우를 방어한다
    if text.lstrip().startswith('{"message"') and "404" in text[:80]:
        return None
    return BriefingFile(day=day, stem=stem, text=text)
