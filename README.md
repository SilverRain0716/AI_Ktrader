# AI_Ktrader

한국 주식 스윙매매 AI 판단 시스템.

일일 증시 브리핑과 정량 지표를 하나의 컨텍스트로 모아 **AI가 매수·보유·매도를 종합 판단**하고, 실행 계층이 검증·리스크 한도를 강제한 뒤 키움 REST API로 주문을 집행한다.

> **현재 단계: Phase 0 검증 + Phase 1 데이터 계층**
> 데이터 계층이 동작한다 (일봉·수급·지표). 키움 REST 실측은 `docs/phase0-verification.md` 참조.

---

## 왜 새로 짓는가

기존 K-Trader(키움 OpenAPI+ 기반)는 3년치 실전 디버깅이 녹아 있는 자산이지만, 두 가지 이유로 그 위에 지을 수 없다.

1. **32비트 COM에 갇혀 있다.** OpenAPI+는 OCX/ActiveX 방식이라 32비트 파이썬이 강제된다. AI 판단 엔진에 필요한 라이브러리 대부분이 동작하지 않는다.
2. **매수 진입이 키움 조건검색식 하나로 고정돼 있다.** 조건식은 코드로 생성·수정·버전관리가 불가능하고 백테스트도 되지 않는다.

K-Trader는 **설계 원칙의 참고 자산**으로 쓴다. 무엇을 계승하고 무엇을 버리는지는 [docs/00-design-v1.md](docs/00-design-v1.md) 5장에 정리돼 있다.

---

## 구조

| 디렉토리 | 역할 |
|---|---|
| `docs/` | 설계 문서, 조사 노트, ADR(결정 기록) |
| `schemas/` | JSON Schema — 계층 간 계약. **여기가 시스템의 척추다** |
| `data/` | 시세·수급·공시 수집, 지표 계산 배치 |
| `briefing/` | 브리핑 텍스트 → 구조화 JSON |
| `decision/` | 컨텍스트 팩 빌더 + AI 판단 엔진 |
| `execution/` | 검증 게이트 + 키움 REST 주문 집행 |
| `backtest/` | 정량 규칙 백테스트 엔진 |

### 데이터 흐름

```
정량 지표 ─┐
브리핑    ─┼─→ 컨텍스트 팩 ─→ AI 판단 ─→ 결정 JSON ─→ 검증 게이트 ─→ 주문
계좌·포지션┘                                              ↑
                                                    여기서 막히면 주문 안 나감
```

**AI는 마지막에 판단한다.** 정량은 AI의 입력이지 AI 출력에 대한 필터가 아니다. 이 순서가 뒤집히면 AI는 후보 생성기로 격하되고, "보유 중 악재 발생 → 청산" 같은 종합 판단을 할 수 없다.

---

## 보안 규칙 (예외 없음)

- **이 저장소는 현재 public이다** ([ADR 0004](docs/adr/0004-repo-visibility.md)). 설계·데이터 계층 단계에 한한 조치이며,
  `execution/`에 코드가 들어오거나 실계좌 설정이 커밋되는 순간 **CI가 빌드를 실패시킨다.** 그때 private으로 전환한다.
- **운용 자금 규모와 리스크 한도는 저장소에 커밋하지 않는다.** 환경변수로만 주입한다. public 여부와 무관한 규칙이다.
- 시크릿은 **환경변수 또는 OS 자격증명 저장소**로만 주입한다. 코드·설정 파일·프롬프트에 하드코딩 금지.
- `.env`는 절대 커밋하지 않는다. `.env.example`만 커밋한다.
- 커밋 전 `pre-commit` 훅이 시크릿 스캔을 돌린다. 훅 없이 커밋하지 않는다.
- **실계좌 주문 코드는 킬 스위치가 구현되기 전까지 머지하지 않는다.**

---

## 개발 단계

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | 키움 REST 앱키 발급, 조건식·유량 제약 실측 | ⏳ 진행 |
| 1 | 데이터 계층 — 일봉·수급 적재 + 지표 계산 | ✅ 1차 완료 |
| 1b | DART 공시 적재, 분봉 일일 적재(키움 REST 필요) | |
| 2 | 브리핑 구조화 — 기존 아카이브 파싱 → JSON | |
| 3 | 백테스트 엔진 — 갭·슬리피지·거래세·거래정지 반영, walk-forward | |
| 4 | AI 판단 엔진 — 컨텍스트 팩 + 출력 스키마 + 페이퍼 실행 | |
| 5 | **페이퍼 forward test — 최소 2~3개월** | |
| 6 | 실행 계층 + 검증 게이트 + 모의투자 주문 | |
| 7 | 소액 실계좌 → 한도 상향 → 완전 자동 | |

**3과 5는 압축할 수 없다.** 3이 없으면 AI가 기여한 부분을 분리할 수 없고, 5가 없으면 검증 자체가 없다.

---

## 데이터 계층 사용법

```bash
pip install -e ".[dev,data]"

python -m data.pipeline listing            # 종목 마스터 + 상장폐지 이력
python -m data.pipeline ohlcv --limit 300  # 일봉 (시총 상위 300)
python -m data.pipeline flows --limit 300  # 기관·외국인 수급
python -m data.pipeline indicators         # 지표 계산
python -m data.pipeline status             # 적재 현황

python -m data.pipeline daily --limit 300  # 운영 배치 (KST 18:30 이후)
```

DB는 `data/warehouse/market.db` (gitignore). `AIK_DATA_DIR`로 위치를 바꿀 수 있다.

계산되는 지표는 `schemas/context_pack.schema.json`의 `$defs.indicators` / `$defs.stockFlows`와
키가 1:1로 대응한다. 지표를 추가할 때는 **스키마를 먼저 고친다.**

테스트: `pytest` (네트워크 테스트는 `pytest -m net -o addopts=""`)

## 문서

- [설계안 v1](docs/00-design-v1.md) — 전체 아키텍처, 판단 사이클, 계승 자산
- [Phase 0 검증 체크리스트](docs/phase0-verification.md) — 실측으로 채워야 할 항목
- [조사: 키움 조건검색식](docs/research/kiwoom-condition-search.md)
- [조사: 데이터 소스](docs/research/data-sources.md)
- [조사: K-Trader 코드 리뷰](docs/research/ktrader-code-review.md)
- [첫 실제 적재에서 드러난 것](docs/02-first-real-run.md) — 버그 3건·설계 오류 2건
- [ADR](docs/adr/) — 왜 그렇게 결정했는가

---

## 면책

투자 판단과 그 결과에 대한 책임은 전적으로 운용자 본인에게 있다. 이 시스템의 출력은 투자 자문이 아니다.
