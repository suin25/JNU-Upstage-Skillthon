# 영수증 가계부 자동화 스킬

영수증 사진 한 장으로 가계부를 자동 완성하고, 소비 패턴 분석과 저축 추천까지 제공하는 스킬.  
Upstage OCR + Information Extract + Solar LLM을 파이프라인으로 연결해 구현했다.

---

## 파이프라인 개요

```
영수증 이미지
    │
    ▼
[scripts/ocr_extract.py]
  ├─ Upstage OCR          → 이미지에서 텍스트 추출
  ├─ Information Extract  → 날짜·가게명·품목·금액 구조화
  └─ Solar LLM            → 소비 카테고리 자동 분류
    │
    ▼
[scripts/analyze.py]
  ├─ 즉시 예산 경고        → 카테고리별 80% / 100% 임계값
  ├─ 소비 속도 예측        → 현재 페이스로 월말 초과 예정일 계산
  ├─ 이상 지출 감지        → 월평균 2배 이상 지출 시 Solar LLM 코멘트
  ├─ 월별 소비 집계        → 카테고리별 지출 현황
  └─ Solar LLM            → 재무 리포트 + 저축 목표 추천

[app.py] — 위 파이프라인을 REST API로 제공하는 FastAPI 웹 서버
```

---

## 파일 구조

```
receipt-ledger-skill/
├── SKILL.md                      # 스킬 정의 및 설계 문서
├── README.md                     # 이 파일
├── app.py                        # FastAPI 웹 서버 진입점
├── ledger_data.json              # 누적 거래 데이터 (자동 생성)
├── static/
│   └── index.html                # 웹 UI (브라우저에서 바로 사용)
├── scripts/
│   ├── config.py                 # 공통 설정·상수·클라이언트 초기화
│   ├── ocr_extract.py            # OCR + 필드 추출 + 카테고리 분류
│   ├── analyze.py                # 소비 분석 + 경고 + 리포트 생성
│   └── main.py                   # 전체 파이프라인 진입점 (CLI)
├── references/
│   └── edge_cases.md             # 예외 상황 처리 가이드
└── assets/
    └── .env.example              # API 키 설정 템플릿
```

---

## 시작하기

### 1. 의존성 설치

```bash
pip install openai requests python-dotenv
```

### 2. API 키 설정

```bash
cp assets/.env.example .env
```

`.env` 파일을 열어 API 키를 입력한다:

```
UPSTAGE_API_KEY=up_xxxxxxxxxxxxxxxxxxxxxxxx
```

> API 키 발급: https://console.upstage.ai → API Keys  
> 첫 가입 시 레퍼럴 코드 **UPWAVE-KOH** 입력하면 $70 크레딧 지급

### 3. 서버 실행 (웹 UI)

```bash
python app.py
# 또는
uvicorn app:app --reload
```

브라우저에서 http://localhost:8080 접속. API 문서는 http://localhost:8080/docs

---

## 사용법

### 영수증 추가 (CLI)

```bash
python scripts/main.py add --image receipt.jpg
python scripts/main.py add --image receipt.jpg --income 4000000
```

출력 예시:
```
[ 영수증 처리 시작 ]
  📷 OCR 처리 중: ../receipt.jpg
  🔍 필드 추출 중 (Information Extract)...
  🏷️  카테고리 분류 중 (Solar LLM)...

─────────────────────────────────────────────
  ✅ 기록 완료
  가게명  : 스타벅스 코리아
  금액    : 7,500원
  카테고리: 카페/음료
  날짜    : 2026-05-11
  결제    : 카드
─────────────────────────────────────────────

⚠️  [카페/음료] 예산 85% 도달
   남은 한도: 15,000원 (한도: 100,000원)
```

### 월별 집계 (CLI)

```bash
python scripts/main.py summary
python scripts/main.py summary --month 2026-04
```

출력 예시:
```
==================================================
  📊 2026-05 소비 현황
==================================================
  총 지출: 1,247,000원

     식비     [████████████░░░░░░░░] 60%  (300,000원 / 500,000원)
  ⚠️  카페/음료 [█████████████████░░░] 85%  ( 85,000원 / 100,000원)
     교통     [████████░░░░░░░░░░░░] 40%  ( 60,000원 / 150,000원)
```

### 월간 재무 리포트 (CLI)

```bash
python scripts/main.py report --income 3000000
python scripts/main.py report --month 2026-04 --income 3500000
```

### 데이터 초기화 (CLI)

```bash
python scripts/main.py reset
```

---

## 개별 스크립트 직접 실행

각 스크립트는 단독으로도 실행 가능하다:

```bash
# OCR + 추출만 테스트
python scripts/ocr_extract.py --image receipt.jpg
python scripts/ocr_extract.py --image receipt.jpg --save

# 분석만 테스트
python scripts/analyze.py --month 2026-05
python scripts/analyze.py --report --income 3000000

# 즉시 경고 테스트
python scripts/analyze.py --alert --store 스타벅스 --amount 92000 --category 카페/음료
```

---

## 예산 커스터마이징

`ledger_data.json`이 생성된 후 `"budget"` 키를 직접 수정하면 된다:

```json
{
  "budget": {
    "식비": 600000,
    "카페/음료": 80000,
    "교통": 200000
  }
}
```

---

## 사용된 Upstage API

| 단계 | API | 용도 |
|------|-----|------|
| OCR | `POST /v1/document-digitization` (model=ocr) | 영수증 이미지 → 텍스트 |
| 구조화 | `information-extract` | 텍스트 → 날짜·가게명·품목·금액 JSON |
| 분류·리포트 | `solar-pro` | 카테고리 분류, 재무 리포트 생성 |

---

## 예외 상황

흐린 이미지, 날짜 누락, 외국어 영수증 등 예외 처리 방법은  
`references/edge_cases.md`를 참고한다.
