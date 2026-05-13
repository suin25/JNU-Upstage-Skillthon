---
name: receipt-ledger
description: >
  영수증 사진 한 장으로 가계부를 자동 완성하는 스킬. Upstage OCR + Document Parse로 영수증을
  구조화하고, Information Extract로 날짜·금액·품목을 추출한 뒤, Solar LLM이 카테고리를 분류하고
  소비 패턴을 분석해 월별 지출 경고와 저축 비율 추천까지 제공한다.
  영수증, 가계부, 지출 분석, 소비 패턴, 절약, 저축, 가계 관리, 예산 초과, 재정 관리 등의
  키워드가 나오면 반드시 이 스킬을 사용한다. 사용자가 "영수증 찍었어", "이번 달 얼마 썼지",
  "지출이 너무 많은 것 같아" 같이 말할 때도 이 스킬을 즉시 트리거한다.
---

# Receipt Ledger Skill — 영수증 가계부 자동화

영수증 이미지를 받아 소비 내역을 자동으로 기록하고, 월별 패턴 분석과 재정 조언을 제공하는 종합 가계부 스킬.

## 전체 파이프라인

```
영수증 이미지 업로드
      │
      ▼
[1] OCR / Document Parse  → 텍스트 추출 (흐릿한 이미지도 처리)
      │
      ▼
[2] Information Extract   → 날짜, 가게명, 품목, 금액 구조화
      │
      ▼
[3] Solar LLM             → 카테고리 분류
      │
      ├─ 이상 지출 감지   → 월평균 2배 이상 지출 시 Solar LLM 코멘트
      │
      ▼
[4] Solar LLM             → 월별 집계 + 경고 + 저축 비율 추천
      │
      ├─ 즉시 예산 경고   → 카테고리별 80% / 100% 임계값
      └─ 소비 속도 예측   → 현재 페이스로 월말 초과 예정일 계산
      │
      ▼
사용자에게 결과 리포트 출력
      │
      ▼
[5] FastAPI 웹 서버 (app.py) → REST API + 브라우저 UI로 위 파이프라인 전부 제공
```

---

## 단계별 구현 가이드

### 사전 준비 — API 키 설정

```python
import os
from dotenv import load_dotenv

load_dotenv()  # 프로젝트 루트의 .env 파일 로드
UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY", "")
if not UPSTAGE_API_KEY:
    raise EnvironmentError("UPSTAGE_API_KEY가 설정되지 않았습니다. 루트의 .env를 확인하세요.")
```

`.env` 파일은 프로젝트 루트(`skillthon/.env`)에 위치한다 (`assets/.env.example` 참고):
```
UPSTAGE_API_KEY=up_xxxxxxxxxxxxxxxxxxxxxxxx
```

API 키는 https://console.upstage.ai → API Keys에서 발급. 첫 가입 시 `UPWAVE-KOH` 레퍼럴 코드로 $70 크레딧 지급.

---

### Step 1 — OCR + Document Parse로 텍스트 추출

영수증 이미지(jpg/png)는 **OCR 모드**를 사용한다. PDF라면 `document-parse`를 그대로 써도 됨.

```python
import requests

def ocr_receipt(image_path: str) -> tuple[str, float]:
    """
    영수증 이미지에서 텍스트와 OCR 신뢰도를 추출한다.

    Returns:
        (추출된 텍스트, OCR 신뢰도 0~1)

    Raises:
        ValueError: 텍스트를 전혀 인식하지 못한 경우
        requests.HTTPError: API 호출 실패
    """
    with open(image_path, "rb") as f:
        response = requests.post(
            "https://api.upstage.ai/v1/document-digitization",
            headers={"Authorization": f"Bearer {UPSTAGE_API_KEY}"},
            files={"document": f},
            data={"model": "ocr"},
            timeout=30
        )
    response.raise_for_status()
    result = response.json()

    confidence: float = result.get("confidence", 1.0)
    text: str = result.get("text", "").strip()

    if not text:
        raise ValueError(
            "영수증 텍스트를 인식하지 못했습니다.\n"
            "→ 이미지가 충분히 밝고 선명한지 확인하세요.\n"
            "→ 영수증이 화면 전체를 채우도록 촬영해 주세요."
        )

    return text, confidence
```

**엣지 케이스 처리:**
- 이미지가 흐리거나 기울어진 경우: `confidence < 0.7`이면 경고 출력 후 처리 계속 진행 (중단 아님)
- 빈 텍스트 반환 시: `ValueError` 발생 → 상위 호출부에서 잡아 사용자에게 안내
- 신뢰도는 `ocr_confidence` 필드로 거래 레코드에 저장해 나중에 필터링 가능

---

### Step 2 — Information Extract로 구조화

OCR 텍스트가 아닌 **영수증 이미지를 base64로 직접** Information Extract에 전달한다. 텍스트 경유보다 구조화 정확도가 높다.

```python
import base64
import json
from openai import OpenAI

# Information Extract 전용 클라이언트 (별도 base_url 사용)
ie_client = OpenAI(
    api_key=UPSTAGE_API_KEY,
    base_url="https://api.upstage.ai/v1/information-extraction"
)

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name":  {"type": "string",  "description": "가게 또는 상호명"},
        "date":        {"type": "string",  "description": "구매 날짜 (YYYY-MM-DD). 영수증에 없으면 빈 문자열."},
        "items": {
            "type": "array",
            "description": "구매 품목 목록",
            "items": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string",  "description": "품목명"},
                    "quantity": {"type": "integer", "description": "수량 (기본 1)"},
                    "price":    {"type": "number",  "description": "단가 (원)"}
                }
            }
        },
        "total_amount":   {"type": "number", "description": "총 결제 금액 (원). '합계' 또는 '결제금액' 기준."},
        "payment_method": {"type": "string", "description": "결제 수단 (카드 / 현금 / 간편결제 / 알 수 없음)"}
    }
}

def extract_fields(image_path: str) -> dict:
    """
    영수증 이미지를 base64로 인코딩해 Information Extract에 직접 전달하고
    구조화된 JSON을 반환한다.

    날짜 누락 → 오늘 날짜 자동 대입
    총액 누락 → 품목 단가 × 수량 합산으로 보정
    """
    with open(image_path, "rb") as f:
        ext  = image_path.rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
        b64  = base64.b64encode(f.read()).decode()

    response = ie_client.chat.completions.create(
        model="information-extract",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        ]}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "receipt_schema", "schema": RECEIPT_SCHEMA}
        }
    )
    receipt: dict = json.loads(response.choices[0].message.content)

    # 날짜 폴백
    if not receipt.get("date"):
        receipt["date"] = datetime.today().strftime("%Y-%m-%d")

    # 총액 폴백
    if not receipt.get("total_amount"):
        receipt["total_amount"] = sum(
            item.get("price", 0) * item.get("quantity", 1)
            for item in receipt.get("items", [])
        )

    return receipt
```

**엣지 케이스 처리:**
- 날짜가 없는 영수증: 오늘 날짜(`YYYY-MM-DD`)로 자동 대입
- 총액이 없는 경우: 품목 단가 × 수량 합산으로 보정 (할인·부가세가 있으면 실제 결제액과 다를 수 있음)
- 품목이 없는 경우(편의점 바코드 등): `store_name` + `total_amount`만으로도 저장 허용

---

### Step 3 — Solar LLM으로 카테고리 분류

```python
CATEGORIES = ["식비", "카페/음료", "교통", "쇼핑/의류", "생활용품", "의료/건강",
              "문화/여가", "교육", "구독서비스", "기타"]

def classify_category(receipt: dict) -> str:
    """영수증 정보를 바탕으로 소비 카테고리를 분류한다."""
    prompt = f"""
다음 영수증 정보를 보고 아래 카테고리 중 하나로만 답하시오. 카테고리명만 출력.
카테고리: {", ".join(CATEGORIES)}

가게명: {receipt.get("store_name")}
품목: {[item["name"] for item in receipt.get("items", [])]}
"""
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20
    )
    category = response.choices[0].message.content.strip()
    return category if category in CATEGORIES else "기타"
```

---

### Step 4 — 월별 집계 + 경고 + 저축 추천

`summarize_month()`로 카테고리별 지출을 집계한 뒤 Solar LLM에 전달해 종합 리포트를 생성한다.
집계는 `generate_report()`가 아닌 `summarize_month()`가 담당하므로, 두 함수를 순서대로 호출한다.

```python
# 1단계: 집계
summary = summarize_month(transactions, month="2026-05", budget=budget)
# summary 구조:
# {
#   "month": "2026-05",
#   "total_spent": 850000,
#   "by_category": {
#       "식비": {"spent": 450000, "limit": 500000, "ratio": 0.9},
#       ...
#   }
# }

# 2단계: Solar LLM 리포트 생성
def generate_report(summary: dict, monthly_income: int) -> str:
    """
    월간 소비 요약(summary)을 바탕으로 Solar LLM이 재무 리포트를 생성한다.

    50/30/20 법칙 기준:
      - 생활비 50%: 식비, 교통, 생활용품, 의료
      - 저축   30%: 적금, 비상금
      - 여가   20%: 카페, 쇼핑, 문화, 구독
    """
    total_spent  = summary["total_spent"]
    by_cat       = summary["by_category"]
    savings_room = max(monthly_income - total_spent, 0)

    # 예산 위험 항목 추출 (80% 이상)
    danger_items = [
        f"{cat}: {info['spent']:,}원 (예산의 {info['ratio']:.0%})"
        for cat, info in by_cat.items()
        if (info["ratio"] or 0) >= 0.8
    ]

    recommended_savings = int(monthly_income * 0.30)  # 수입의 30%

    prompt = f"""
당신은 친절하고 실용적인 개인 재무 코치입니다.
아래 {summary['month']} 소비 데이터를 바탕으로 한국어로 재무 리포트를 작성하세요.

[이번 달 수입] {monthly_income:,}원
[총 지출] {total_spent:,}원 (수입의 {total_spent/monthly_income:.0%})
[카테고리별 지출]
{chr(10).join(f"  - {cat}: {info['spent']:,}원" for cat, info in by_cat.items())}
[예산 위험 항목]
{chr(10).join(danger_items) if danger_items else "없음 — 모든 카테고리 양호"}
[저축 여력] {savings_room:,}원
[권장 저축액 (수입의 30%)] {recommended_savings:,}원

리포트 구성 (반드시 이 순서대로):
1. 한 줄 총평 — 이번 달 소비를 긍정/부정 솔직하게 평가
2. 위험 항목 절약 팁 — 있다면 카테고리별로 구체적인 방법 1~2가지
3. 50/30/20 비율 평가 — 현재 비율과 권장 비율 비교
4. 다음 달 저축 목표 — 구체적인 금액과 실천 방법 1가지

친근하고 구체적으로, 600자 이내로 작성하세요.
"""
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900
    )
    return response.choices[0].message.content.strip()
```

---

### Step 5 — 전체 실행 흐름

`ocr_extract.py`의 `process_image()`가 Step 1~3을 묶어 실행하고, 이후 `analyze.py`의 `check_budget_alert()`로 즉시 경고를 확인한다.

```python
def process_image(image_path: str) -> dict:
    """
    이미지 경로를 받아 OCR → 필드 추출 → 카테고리 분류까지 실행하고
    완성된 영수증 레코드를 반환한다.

    반환 레코드 예시:
    {
        "store_name": "스타벅스 코리아",
        "date": "2026-05-13",
        "items": [...],
        "total_amount": 7500,
        "payment_method": "카드",
        "category": "카페/음료",
        "ocr_confidence": 0.94   # OCR 신뢰도 (0~1)
    }
    """
    text, confidence = ocr_receipt(image_path)

    if confidence < 0.7:
        print(f"  ⚠️  OCR 신뢰도 낮음 ({confidence:.0%}) — 결과가 부정확할 수 있습니다.")

    receipt = extract_fields(image_path)
    receipt["category"]        = classify_category(receipt)
    receipt["ocr_confidence"]  = round(confidence, 3)  # 레코드에 신뢰도 저장

    return receipt


def check_budget_alert(receipt: dict, transactions: list, budget: dict) -> str | None:
    """단건 추가 후 해당 카테고리의 이번 달 누적 지출을 확인해 경고 메시지를 반환한다."""
    from collections import defaultdict
    cat   = receipt.get("category", "기타")
    limit = budget.get(cat)
    if not limit:
        return None

    month = receipt.get("date", "")[:7]
    totals: dict[str, int] = defaultdict(int)
    for t in transactions:
        if t.get("date", "")[:7] == month:
            totals[t["category"]] += t.get("total_amount", 0)

    spent = totals.get(cat, 0)
    ratio = spent / limit

    if ratio >= 1.0:
        over = spent - limit
        return (
            f"🚨 [{cat}] 예산 초과!\n"
            f"   이번 달 지출: {spent:,}원 / 한도: {limit:,}원 (+{over:,}원 초과)"
        )
    elif ratio >= 0.8:
        remain = limit - spent
        return (
            f"⚠️  [{cat}] 예산 {ratio:.0%} 도달\n"
            f"   남은 한도: {remain:,}원 (한도: {limit:,}원)"
        )
    return None
```

---

### Step 6 — 이상 지출 감지 (Solar LLM)

영수증 1건을 추가한 직후, 같은 카테고리 이번 달 평균 대비 2배 이상이면 Solar LLM이 한 문장 코멘트를 생성한다.

```python
def check_anomaly(receipt: dict, transactions: list) -> str | None:
    """월평균 2배 이상 지출 시 Solar LLM 코멘트를 반환한다. 이전 거래가 2건 미만이면 건너뛴다."""
    cat    = receipt.get("category", "기타")
    amount = receipt.get("total_amount", 0)
    month  = receipt.get("date", "")[:7]
    rid    = receipt.get("id")

    prev = [
        t.get("total_amount", 0)
        for t in transactions
        if t.get("category") == cat
        and t.get("date", "")[:7] == month
        and t.get("id") != rid
    ]

    if len(prev) < 2 or not amount:
        return None

    avg = sum(prev) / len(prev)
    if avg == 0 or amount < avg * 2:
        return None

    prompt = (
        f"이번 달 {cat} 평균 지출은 {avg:,.0f}원인데, "
        f"방금 {receipt.get('store_name', '이 가게')}에서 {amount:,.0f}원을 사용했습니다 ({amount / avg:.1f}배). "
        f"이 지출이 합리적인지 친근하게 한 문장으로만 평가하세요."
    )
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=80,
    )
    return f"🔍 {response.choices[0].message.content.strip()}"
```

**엣지 케이스 처리:**
- 이전 거래 2건 미만: 평균 계산 불가로 건너뜀
- 평균이 0인 경우: 제로 나누기 방지, 건너뜀

---

### Step 7 — 소비 속도 기반 예산 초과 예측

현재 일별 평균 지출 속도로 월말까지 예산 초과가 예상되는 카테고리를 계산해 경고한다.

```python
def check_pace_alert(transactions: list, month: str, budget: dict) -> list[str]:
    """현재 페이스로 월말 예산 초과가 예상되는 카테고리 목록을 반환한다."""
    year, mon = map(int, month.split("-"))
    days_in_month = monthrange(year, mon)[1]
    today = datetime.today()

    days_elapsed = today.day if today.strftime("%Y-%m") == month else days_in_month

    monthly = [t for t in transactions if t.get("date", "")[:7] == month]
    totals: dict[str, float] = defaultdict(float)
    for t in monthly:
        totals[t["category"]] += t.get("total_amount", 0)

    warnings = []
    for cat, limit in budget.items():
        spent = totals.get(cat, 0)
        if spent == 0 or spent >= limit:   # 데이터 없거나 이미 초과면 건너뜀
            continue
        daily_rate = spent / days_elapsed
        projected  = daily_rate * days_in_month
        if projected > limit:
            days_until  = int((limit - spent) / daily_rate)
            exceed_date = today + timedelta(days=days_until)
            warnings.append(
                f"📈 [{cat}] 현재 속도면 {exceed_date.strftime('%m월 %d일')}에 예산 초과 예정"
                f" (월말 예상: {projected:,.0f}원 / 한도: {limit:,}원)"
            )
    return warnings
```

출력 예시:
```
📈 [카페/음료] 현재 속도면 05월 25일에 예산 초과 예정 (월말 예상: 124,000원 / 한도: 100,000원)
```

---

### Step 8 — FastAPI 웹 서버 (app.py)

CLI 파이프라인을 REST API로 감싸 브라우저에서 바로 사용할 수 있게 한다.

```
실행:  python app.py  (또는 uvicorn app:app --reload)
UI:    http://localhost:8080
Docs:  http://localhost:8080/docs
```

**주요 엔드포인트:**

| 메서드 | 경로 | 기능 |
|--------|------|------|
| `POST` | `/api/receipt` | 영수증 이미지 업로드 → OCR + 분류 + 즉시 경고 |
| `POST` | `/api/entry` | 수동 지출 입력 (이미지 없이 직접 기록) |
| `GET` | `/api/summary` | 월별 카테고리 집계 (기본: 이번 달) |
| `POST` | `/api/report` | Solar LLM 월간 재무 리포트 생성 |
| `GET` | `/api/transactions` | 거래 목록 조회 (월 필터 가능) |
| `DELETE` | `/api/transactions/{id}` | 특정 거래 삭제 |
| `GET/PUT` | `/api/budget` | 예산 한도 조회 및 수정 |
| `GET` | `/api/categories` | 지원 카테고리 목록 조회 |
| `GET` | `/api/status` | API 키 설정 여부 확인 |
| `DELETE` | `/api/reset` | 전체 데이터 초기화 |
| `POST` | `/api/setup` | API 키 설정 (런타임 즉시 반영) |

`POST /api/receipt` 응답 예시:
```json
{
  "receipt": {
    "store_name": "스타벅스 코리아",
    "date": "2026-05-13",
    "total_amount": 7500,
    "category": "카페/음료",
    "payment_method": "카드",
    "ocr_confidence": 0.94
  },
  "alert": "⚠️  [카페/음료] 예산 85% 도달\n   남은 한도: 15,000원 (한도: 100,000원)",
  "pace_alerts": ["📈 [카페/음료] 현재 속도면 05월 25일에 예산 초과 예정"],
  "anomaly": null,
  "message": "스타벅스 코리아 7,500원 — 카페/음료 기록 완료"
}
```

**엣지 케이스 처리:**
- 이미지가 아닌 파일 업로드: `400 Bad Request` 반환
- OCR 실패: `422 Unprocessable Entity` + 사유 메시지
- 거래 없는 월의 리포트 요청: `404 Not Found`
- 서버 기동 시 기존 데이터의 UUID 자동 backfill

---

## 출력 예시

**영수증 1장 처리 결과:**
```
✅ 스타벅스 코리아 7,500원 — 카페/음료 기록 완료
⚠️  카페/음료 예산 80% 도달. 남은 한도: 20,000원
```

**월별 리포트 일부:**
```
이번 달 식비로 487,000원을 쓰셨네요. 예산(500,000원)의 97%로
거의 다 쓰셨어요! 남은 며칠은 집밥 위주로 가시면 좋겠습니다.

수입 300만원 기준 권장 저축액은 90만원인데, 현재 총 지출이
240만원으로 저축 여력은 약 60만원입니다. 이번 달은 목표보다
30만원 부족해요.

다음 달 목표: 카페 지출을 주 2회로 줄이면 월 3만원 추가 절약 가능!
```

---

## 레퍼런스

- Upstage API 전체 스펙: https://console.upstage.ai/api/docs/for-agents/raw
- OCR 엔드포인트: `POST https://api.upstage.ai/v1/document-digitization` (model=ocr)
- Information Extract: `model="information-extract"` via OpenAI-compatible client
- Solar LLM: `model="solar-pro"` via OpenAI-compatible client
- Upstage Cookbook (예제 모음): https://github.com/UpstageAI/cookbook

더 자세한 API 파라미터나 응답 형식이 필요하면 `./references/` 폴더를 참고하거나
위 공식 문서 URL을 직접 조회한다.
