"""schemas/ 아래 JSON Schema가 스스로 유효한지 검사한다.

CI에서 돌린다. 스키마가 계층 간 계약이므로, 깨진 스키마가 머지되면
데이터·판단·실행이 조용히 어긋난다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def main() -> int:
    files = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if not files:
        print("schemas/ 에 스키마가 없습니다.", file=sys.stderr)
        return 1

    failed = False
    for path in files:
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"FAIL {path.name}: JSON 파싱 실패 — {e}", file=sys.stderr)
            failed = True
            continue

        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:
            print(f"FAIL {path.name}: 스키마 무효 — {e}", file=sys.stderr)
            failed = True
            continue

        print(f"OK   {path.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
