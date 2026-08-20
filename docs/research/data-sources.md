# 조사: 정량 데이터 소스

조사일: 2026-08-20 (실제 호출 검증 포함)

---

## 0. 2026년의 지형 변화 — KRX가 로그인 벽을 세웠다

**2025-12-26부터 `data.krx.co.kr`이 로그인 없이는 통계 데이터를 주지 않는다.** 실측:

```
POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
  bld=dbms/MDC/STAT/standard/MDCSTAT01501 (전종목 시세) → 본문 "LOGOUT"
  GET /comm/fileDn/GenerateOTP/generate.cmd (CSV OTP)   → "LOGOUT"
  bld=dbms/comm/finder/finder_stkisu (종목코드 검색)     → 정상 (유일하게 열림)
```

- `short.krx.co.kr`(공매도종합포털)도 `data.krx.co.kr`로 302 리다이렉트 → **공매도도 같은 벽 뒤**
- **pykrx 1.2.8은 `KRX_ID` / `KRX_PW` 환경변수 필수**로 바뀌었다. 없으면 대부분 빈 DataFrame 또는 KeyError. (GitHub 마일스톤 "KRX 로그인 방식 변경 대응" 2026-01-31 완료)
- 대체로 열린 **KRX Open API(openapi.krx.co.kr)** 는 일봉만 있고 **수급·프로그램매매·공매도·시가총액 시계열·PER/PBR이 전혀 없다.** 약관 제6조 ②가 **상업적 이용을 금지**한다.

---

## 1. 일봉 OHLCV

### ★ 네이버 `siseJson` — 현재 가장 실용적

```
GET https://api.finance.naver.com/siseJson.naver
    ?symbol=005930&requestType=1&startTime=19900101&endTime=20260820&timeframe=day
→ 9,452행 (1990-01-03~), 539KB, 2.3초, 단일 호출
```

- 컬럼: `날짜, 시가, 고가, 저가, 종가, 거래량, 외국인소진율`
- **수정주가 반영됨**, 인증 불필요, 페이지네이션 불필요, 기간 상한 없음
- `timeframe=week` / `month`도 동작
- ⚠️ 비공식 API. 언제든 차단 가능하고 약관상 근거가 없다. 호출 간격 조절 필수.
- ⚠️ 해외 IP에서의 접근 가능 여부는 [Phase 0 E1](../phase0-verification.md)에서 확인 필요

### FinanceDataReader 0.9.202 — **치명적 결함 있음**

```
DataReader("005930","2000-01-01","2026-08-20") → 3000행, 시작일이 2014-05-29로 잘림
DataReader("005930","2010-01-01","2012-01-01") →    0행, 빈 DataFrame (예외 없음!)
```

**3,000행 상한 + 무경고 절단.** 2014-05-29 이전을 요청하면 에러 없이 데이터가 사라진다. 같은 구간을 네이버로 받으면 1,976행이 나온다.

→ **장기 백테스트 원천으로 쓰면 안 된다.** 종목 마스터·상장폐지 목록 용도로만 쓴다.

### KRX Open API

- 신청 4단계(가입 → 인증키 신청 → 승인 → 서비스별 활용신청 → 승인)
- **1키당 1일 10,000회**, 12개월 미사용 시 무통보 삭제, 이용기간 1년
- **상업적 이용 금지** (약관 제6조 ②), 출처 명시 의무, 제3자 제공 금지
- 데이터 시작 2010-01-04, **미수정 주가**
- 래퍼: `pykrx-openapi` 0.1.1, `krx-openapi` 0.1.0

---

## 2. 분봉 — 무료 소급 수집 불가

| 소스 | 실측 결과 |
|---|---|
| 네이버 분봉 | **시가·고가·저가가 전부 `null`**, 종가·거래량만. **최근 6거래일치뿐** |
| KIS `주식당일분봉조회` | 1회 30건, 루프 돌려도 **당일치만** |
| 키움 REST `ka10080` | 과거 조회 깊이 **확인 불가** → Phase 0 D2에서 실측 |
| KRX 유료 | 1년치 약 30만원, 1996~2021 전체 약 377만원 (2차 출처, 최신가 확인 필요) |

→ **오늘부터 매일 장 마감 후 당일 분봉을 적재해 스스로 축적하는 것 외에 방법이 없다.** 지금 시작하면 3개월 뒤 60거래일치.

---

## 3. 수급

| 데이터 | 경로 | 상태 |
|---|---|---|
| 종목별 기관·외국인 | `finance.naver.com/item/frgn.naver?code=` | ✅ 무인증. 페이지당 20일치. **개인 수급은 없음** |
| 시장 전체 투자자별 | `finance.naver.com/sise/investorDealTrendDay.naver` | ✅ 개인/외국인/기관계 + 기관 세분류 |
| 프로그램매매 | 키움 REST `ka90005~ka90013` | 계좌 필요 |
| 공매도 | 키움 REST `ka10014` | 계좌 필요 |
| 전 종목 정밀 수급 | pykrx + KRX 로그인 | ⚠️ 개인 계정 자동 로그인은 약관 회색지대 |
| 대차잔고 | KOFIA FreeSIS | 사이트는 열리나 **JSON API 경로 확인 불가** (WebSquare SPA) |
| 네이버 프로그램매매 | `sise/programDealTrend.naver` | ❌ 404 (페이지 삭제됨) |

---

## 4. 재무·공시

### DART OpenAPI — 가장 안정적

- **1일 20,000건** (에러코드 `020`), 다중회사 조회 최대 100건 (`021`)
- API 그룹 6개, 약 85개 엔드포인트: 공시정보 4 / 정기보고서 주요정보 30 / **재무정보 7** / 지분공시 2 / 주요사항보고서 36 / 증권신고서 6
- `corp_code` 없이 조회 시 **검색기간 3개월 제한**, 페이지당 100건
- 클라이언트: `OpenDartReader` 0.3.3 (⚠️ **Python 3.13+ 요구**) / `dart-fss` 0.4.17 (Python 3.7+)

### 컨센서스 — 가능하지만 저작권 경고

- 구 `comp.fnguide.com/SVO2/ASP/...` 계열은 **전부 죽었다** (302 → wcomp, 또는 503)
- 신버전 `wcomp.fnguide.com`은 무로그인으로 열리고 JSON 엔드포인트 노출:
  `POST /Consensus/getScrGap` → 종목코드·목표주가·전일종가·괴리율·추정기관수
- 산출 기준: *"최근 3개월 이내에 3개 증권사 이상 의견을 제시한 종목"*
- ⚠️ FnGuide 명시: *"데이터 베이스화 할 경우 민형사상 책임을 물을 수 있습니다."*
  → **개인 연구 한정. DB 적재 후 서비스화는 금지.** 상업화 시 FnGuide 유료 계약 필요.

---

## 5. 상장폐지·거래정지 (생존편향 방지)

### ★ 상장폐지 — FDR로 해결

```python
fdr.StockListing("KRX-DELISTING")  # → (4176, 15)
```
- 기간 **1960-11-21 ~ 2026-08-20** (당일 갱신)
- 컬럼: `Symbol, Name, Market, ListingDate, DelistingDate, Reason, Industry, ToSymbol, ToName` 등
- `Reason`이 정성적으로 유용 (감사의견 의견거절, 자본전액잠식, 피흡수합병 등)
- `ToSymbol`/`ToName`으로 합병 승계 종목 추적 → 연속 시계열 재구성 가능
- **상폐 종목의 과거 주가도 받아진다** (검증: 한진해운 117930 → FDR 1,776행 / 네이버 105KB)

### 거래정지 — 정식 이력 소스 없음

- `fdr.StockListing("KRX-ADMINISTRATIVE")` → **`ValueError: No tables found`로 깨져 있음**
- KIND(`kind.krx.co.kr`)의 매매거래정지 페이지 → 조사 환경에서 **Akamai 403** (한국 IP에서는 될 수 있음)
- **실전 우회법**: 일봉에서 `거래량 == 0` 이고 `시가=고가=저가=0`인 날 = 거래정지일. 백테스트 체결 가능성 판정에는 충분하다.

---

## 6. 지표 계산 라이브러리 (설치 검증 완료)

| 라이브러리 | 버전 | 설치 | 판정 |
|---|---|---|---|
| **TA-Lib** | 0.7.1 (2026-07-16) | `pip install TA-Lib` **한 줄** (manylinux 휠, C 컴파일 불필요) | ✅ **채택** |
| **pandas-ta-classic** | 0.6.52 (2026-06-24) | `pip install pandas-ta-classic` | ✅ **채택** (accessor 방식이 편리) |
| pandas-ta (원본) | 0.4.71b0 | Python **3.12+** 요구, 베타 2개만 남음 | ⚠️ 배제 |
| ta (bukosabino) | 0.11.0 (2023-11) | **빌드 실패** (setuptools 비호환) | ❌ 배제 |

검증: `talib` 함수 161개, `RSI(14)=55.05, MACD=-6461.65, ADX(14)=21.90` (005930 실데이터)

---

## 7. 정합성 함정

1. **소스를 섞지 마라.** 네이버·FDR은 수정주가, KRX Open API·pykrx(`adjusted=False`)는 원본가. 삼성전자 2018-04-27 종가가 각각 53,000 / 2,650,000. 섞으면 수익률이 50배 튄다.
2. **거래정지일 0값 행.** 2018-04-30~05-03이 `시가=고가=저가=0, 거래량=0, 종가=전일종가`로 들어온다. 지표에 넣으면 ATR·볼린저·RSI가 오염된다. → `Open>0 & Volume>0` 필터 필수 (동시에 거래정지 판별 신호로 재활용).
3. **pykrx 수정주가는 "요청 구간 마지막 날 기준"으로 재계산된다.** 캐시를 증분 갱신하면 분할 이벤트 후 조용히 불일치가 쌓인다. → 분할·증자 시 해당 종목 전체 재적재.
4. **FDR 3,000행 절단 검증.** 적재 후 `len(df)`와 요청 기간을 비교하는 단계를 반드시 넣는다.

---

## 8. 권장 구성

```
일봉 OHLCV   네이버 siseJson (1990~, 수정주가, 무인증)
             └ 검증용 대조: 키움 REST ka10081
종목 마스터   FDR StockListing("KRX" / "KRX-DESC")
상장폐지     FDR StockListing("KRX-DELISTING")     ★ 생존편향 해결
수급         네이버 frgn.naver + investorDealTrendDay
             프로그램·공매도: 키움 REST ka90005~, ka10014
재무·공시     DART OpenAPI (20,000건/일)
컨센서스      wcomp.fnguide.com  ※ 개인 연구 한정, DB화 금지
지표         TA-Lib 0.7.1 + pandas-ta-classic 0.6.52
분봉         무료 소급 불가 → 오늘부터 키움 REST로 당일 분봉 매일 적재
```

**리스크 순위**: ① KRX 로그인 벽 → ② 네이버 비공식 API 의존 → ③ FnGuide 저작권 → ④ 분봉 부재

---

## Sources

- [pykrx](https://github.com/sharebook-kr/pykrx) · [Issue #244](https://github.com/sharebook-kr/pykrx/issues/244)
- [FinanceDataReader](https://github.com/financedata-org/FinanceDataReader)
- [KRX Open API 이용약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp) · [서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)
- [OpenDART 개발가이드](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- [FnGuide 컨센서스](https://wcomp.fnguide.com/CompanyInfo/Consensus?gicode=A005930)
- [네이버 종목별 외국인·기관](https://finance.naver.com/item/frgn.naver?code=005930)
- [TA-Lib](https://pypi.org/project/TA-Lib/) · [pandas-ta-classic](https://github.com/xgboosted/pandas-ta-classic)
- [키움 REST API 가이드](https://openapi.kiwoom.com/m/guide/apiguide)
