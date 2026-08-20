# 조사: 키움 조건검색식 활용 범위

조사일: 2026-08-20 · 결론은 [ADR 0002](../adr/0002-condition-search-as-sensor.md)

---

## 1. 지원 범위

조건 대분류 6개: **범위지정 / 시세분석 / 기술적분석 / 패턴분석 / 재무분석 / 순위분석**
(출처: [조건검색 개별조건 도움말 PDF 68p](https://download.kiwoom.com/hero4_help_doc/KiwoomHero4_AdvancedSearch.pdf))

### 기술적 지표

| 분류 | 지표 |
|---|---|
| 추세 | MACD, MACD Signal/OSC, Price Oscillator, LRS/LRL, TSF, EOM, Parabolic, VHF |
| 모멘텀 | 이격도, CCI, Chaikin's Osc, Momentum, Stochastic(fast/slow), ROC, Williams %R, TRIX, Mass Index, Band %b |
| 이동평균 | 주가이평 돌파, 배열(3개/4개), 거래량이평 돌파. **계산방식 6종**(단순/지수/가중/기하/조화/삼각) |
| 채널 | Envelope, Bollinger Band, Band Width, Pivot, **일목균형표**, Price Channel |
| 변동성 | DMI, ADX, **RSI**, Standard Deviation, Sigma, **True Range** |
| 거래량 | A/D선, MFI, VR, Volume Oscillator, OBV, PVI, Demark, 삼선전환도 |

- **ATR은 없다.** True Range만 있고 평균화(ATR)와는 다르다. → ATR 기반 손절폭 산출은 자체 계산 필요.
- DEMA, TEMA는 공식 FAQ에서 미제공 명시.

### 수급 (국내 최고 수준)

외국인/기관/개인/기타법인/연기금/국가 및 조합. 순매수 **일수·수량·금액·누적**, 상장주식수 대비 비율, 거래량 대비 비율. 외국인지분율 및 변동·추세. **프로그램매매** 순매매일수·증감수량/금액·거래량 대비 비율. **공매도** 거래량·대금·누적 비중·평균가 대비 등락률. **대차잔고** 증감·연속봉수·유통주식수 대비 비중. 거래원별 순매매·외국계 비중.

### 재무

PER, PBR, PSR, PCR, EV/EBITDA, PEG, EPS, BPS, 배당수익률, 영업이익률, 순이익률, ROE, ROA, 총자산회전율, 유보율, 매출액·영업이익·순이익·EPS 증감률, 부채비율, 이자보상배율 등.

### 조합·필터

- AND / OR / NOT, **괄호 중첩 가능**. 단 괄호 사용 시 순차검색 불가.
- 조건 개수 상한 **20개** ([0150] 도움말)
- 종목 필터: 정리매매·관리·투자위험·투자경고·환기·단기과열 제외. 우선주·ETF·REITs 구분.
- **스팩(SPAC) 전용 제외 옵션은 확인 불가** → 애플리케이션 후처리 필터 필요.
- HTS 검색결과 보관 한도 **682종목**.

---

## 2. 하드 제약

| # | 제약 | 출처 |
|---|---|---|
| 1 | **조건식 생성·수정은 영웅문4에서만.** API로 생성·수정·정의 조회 불가 | 키움 REST 가이드: *"조건검색은 영웅문4에서 만드실 수 있습니다."* / OpenAPI+ 개발가이드 9.1 |
| 2 | **최소 판정 주기 1분** (틱 불가) | 공식 FAQ: *"조건식에서는 최소 주기가 1분 단위이므로 순간체결량 조건은 설정 불가"* |
| 3 | **실시간 등록 세션당 10건** | 키움 REST 유량 문서 / OpenAPI+ 개발가이드 |
| 4 | **결과 100종목 초과 시 실시간 신호 중단** | KOA Studio 개발가이드: *"조건검색 결과가 100종목을 넘게 되면 실시간 조건검색을 할 수가 없습니다."* |
| 5 | **재무는 최근 결산·최근 분기만** | 공식 FAQ: *"이전 기간에 대한 조건식을 설정할 수 없습니다"* |
| 6 | 재무 데이터는 FnGuide 제공, **실적 발표 후 2~3주 지연**, 연결재무제표 고정 | 공식 FAQ |
| 7 | **당일 투자자 수급은 장 종료 후 제공** | 공식 FAQ |
| 8 | **분봉 주기는 수정주가 미반영** | 공식 FAQ: *"분주기에서는 수정주가를 반영하지 않고 있습니다"* |
| 9 | 조건만족시간이 **재편입 시 최초 시각을 덮어씀** | 공식 FAQ |
| 10 | 실시간 가동 시간: KRX 조건식 09:00~15:30, 통합 08:00~20:00 | 공식 FAQ |
| 11 | 시간외단일가 조건식 미제공 | 공식 FAQ |
| 12 | 차트 패턴(쌍바닥·눌림목) 조건 불가 | 공식 FAQ |
| 13 | **백테스트 불가** — 과거 시점 조건 만족 여부 재현 수단 없음 | — |

---

## 3. API 인터페이스

### OpenAPI+ (COM)
`GetConditionLoad()` → `OnReceiveConditionVer` → `GetConditionNameList()` → `SendCondition(scr, name, idx, nSearch)` → `OnReceiveTrCondition`(최초 목록) / `OnReceiveRealCondition`(실시간 I/D)
에러: `-11` 조건번호 없음, `-12` 조건번호·조건식 불일치, `-13` 조회요청 초과

### REST/WebSocket (신규)
| API | 이름 | trnm |
|---|---|---|
| ka10171 | 조건검색 목록조회 | CNSRLST |
| ka10172 | 조건검색 요청 일반 | CNSRREQ (search_type "0") |
| ka10173 | 조건검색 요청 실시간 | CNSRREQ (search_type "1") |
| ka10174 | 조건검색 실시간 해제 | CNSRCLR |

- 엔드포인트: `wss://api.kiwoom.com:10000/api/dostk/websocket` (모의 `wss://mockapi.kiwoom.com:10000`)
- 실시간 수신: `trnm:"REAL"`, `type:"02"`, `values."843"` = `I`/`D`, `"9001"` 종목코드
- **거래소구분 `stex_tp`는 `K:KRX`만** 정의됨
- 일반조회는 `cont_yn`/`next_key` 연속조회 지원. 응답에 종목코드 + 종목명 + 현재가 + 등락율 + 거래량 포함 (OpenAPI+가 종목코드만 주던 것보다 개선)
- **목록조회(ka10171)를 먼저 해야 실시간 조회 가능** (공식 명시)

⚠️ **REST 문서에는 "10개 제한"과 "100종목 제한"이 명시돼 있지 않다.** 백엔드가 같으므로 동일할 가능성이 높지만 근거가 없다 → [Phase 0 C2/C3](../phase0-verification.md)에서 실측.

---

## 4. 실사용자 보고 한계

- **편입 1회성 처리 문제** — 편입/이탈/재편입 상태를 자체 상태머신으로 관리해야 함
- **조건식 미등록 시 크래시** — `GetConditionNameList()`가 빈 문자열 반환 → IndexError
- **WebSocket 재연결 미구현 시 조용한 사망** — 봇이 시세 수신 중인 줄 알고 정지
- **순위조건 결과 축소** — 상위 100위를 걸어도 제외조건 필터링으로 실제 검출 수가 줄어듦
- **외국인 데이터 소스 이원화** — 당일(거래소 제공)과 익일(외국인한도시스템 확정치)의 값이 달라 같은 조건식도 결과가 달라짐

---

## Sources

- [조건검색 개별조건 도움말 (공식 PDF)](https://download.kiwoom.com/hero4_help_doc/KiwoomHero4_AdvancedSearch.pdf)
- [키움 OpenAPI+ 개발가이드 v1.5](https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.5.pdf)
- [키움 REST API 가이드 — 조건검색](https://openapi.kiwoom.com/m/guide/apiguide?jobTpCode=15)
- [Kiwoom-Securities/Kiwoom-REST-API (공식 저장소)](https://github.com/Kiwoom-Securities/Kiwoom-REST-API)
- [[0150] 조건검색 도움말](https://download.kiwoom.com/hero4_help_new/0150.htm)
- [[0156] 조건검색실시간 도움말](https://download.kiwoom.com/hero4_help_new/0156.htm)
- [KOA Studio 개발가이드 미러](https://github.com/me2nuk/stockOpenAPI/blob/main/README.md)
- [koapy RateLimiter (조건검색 제한 인용)](https://github.com/elbakramer/koapy/blob/master/koapy/backend/kiwoom_open_api_plus/core/KiwoomOpenApiPlusRateLimiter.py)
- [키움 Open API+ 서비스 소개 (호출 제한)](https://www.kiwoom.com/h/customer/download/VOpenApiInfoView)
- [퀀트투자를 위한 키움증권 API](https://wikidocs.net/79241)
