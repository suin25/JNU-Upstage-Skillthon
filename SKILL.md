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
[3] Solar LLM             → 카테고리 분류 + 이상 소비 감지
      │
      ▼
[4] Solar LLM             → 월별 집계 + 경고 + 저축 비율 추천
      │
      ▼
사용자에게 결과 리포트 출력
```

---

## 단계별 구현 가이드

### 사전 준비 — API 키 설정

```python
import os
from dotenv import load_dotenv

load_dotenv()
UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY")
if not UPSTAGE_API_KEY:
    raise EnvironmentError("UPSTAGE_API_KEY가 설정되지 않았습니다. ./assets/.env를 확인하세요.")
```

`.env` 파일 형식 (./assets/.env.example 참고):
```
UPSTAGE_API_KEY=up_xxxxxxxxxxxxxxxxxxxxxxxx
```

API 키는 https://console.upstage.ai → API Keys에서 발급. 첫 가입 시 `UPWAVE-KOH` 레퍼럴 코드로 $70 크레딧 지급.

---

### Step 1 — OCR + Document Parse로 텍스트 추출

영수증 이미지(jpg/png)는 **OCR 모드**를 사용한다. PDF라면 `document-parse`를 그대로 써도 됨.

```python
import requests

def extract_text_from_receipt(image_path: str) -> str:
    """영수증 이미지에서 텍스트를 추출한다."""
    with open(image_path, "rb") as f:
        response = requests.post(
            "https://api.upstage.ai/v1/document-digitization",
            headers={"Authorization": f"Bearer {UPSTAGE_API_KEY}"},
            files={"document": f},
            data={"model": "ocr"}
        )
    result = response.json()

    # confidence가 낮으면 경고
    confidence = result.get("confidence", 1.0)
    if confidence < 0.7:
        print(f"⚠️  OCR 신뢰도가 낮습니다 ({confidence:.0%}). 이미지를 다시 촬영해 주세요.")

    return result.get("text", "")
```

**엣지 케이스 처리:**
- 이미지가 흐리거나 기울어진 경우: confidence 값을 체크해 사용자에게 재촬영 안내
- 빈 텍스트 반환 시: "영수증 인식에 실패했습니다" 메시지 출력 후 종료
- 영수증이 여러 장인 경우: 이미지 배열을 받아 루프 처리

---

### Step 2 — Information Extract로 구조화

OCR 텍스트를 기반으로 날짜·가게명·품목·금액을 JSON으로 추출한다.

```python
from openai import OpenAI

client = OpenAI(
    api_key=UPSTAGE_API_KEY,
    base_url="https://api.upstage.ai/v1"
)

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name":  {"type": "string",  "description": "가게 또는 상호명"},
        "date":        {"type": "string",  "description": "구매 날짜 (YYYY-MM-DD)"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string",  "description": "품목명"},
                    "quantity": {"type": "integer", "description": "수량"},
                    "price":    {"type": "number",  "description": "단가 (원)"}
                }
            },
            "description": "구매 품목 목록"
        },
        "total_amount": {"type": "number", "description": "총 결제 금액 (원)"},
        "payment_method": {"type": "string", "description": "결제 수단 (카드/현금/간편결제)"}
    }
}

def extract_receipt_fields(ocr_text: str) -> dict:
    """OCR 텍스트에서 영수증 필드를 구조화된 JSON으로 추출한다."""
    response = client.chat.completions.create(
        model="information-extract",
        messages=[{
            "role": "user",
            "content": ocr_text
        }],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "receipt_schema",
                "schema": RECEIPT_SCHEMA
            }
        }
    )
    import json
    return json.loads(response.choices[0].message.content)
```

**엣지 케이스 처리:**
- 날짜가 없는 영수증: 오늘 날짜로 자동 대입하고 사용자에게 확인 요청
- 총액이 품목 합산과 다를 경우: total_amount 우선 사용 + 경고 로그
- 품목이 없는 경우(편의점 바코드 등): store_name + total_amount만으로도 저장 허용

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

소비 내역이 누적된 후 Solar LLM으로 종합 분석 리포트를 생성한다.

```python
def generate_monthly_report(
    transactions: list[dict],
    monthly_income: int,
    budget_limits: dict  # 예: {"식비": 400000, "쇼핑/의류": 200000}
) -> str:
    """월별 소비 내역을 분석해 경고와 저축 추천을 포함한 리포트를 생성한다."""

    # 카테고리별 집계
    from collections import defaultdict
    totals = defaultdict(int)
    for t in transactions:
        totals[t["category"]] += t["total_amount"]
    total_spent = sum(totals.values())

    # 예산 초과 항목 탐지
    warnings = []
    for cat, limit in budget_limits.items():
        spent = totals.get(cat, 0)
        ratio = spent / limit if limit > 0 else 0
        if ratio >= 0.8:
            warnings.append(f"{cat}: {spent:,}원 사용 (예산의 {ratio:.0%})")

    prompt = f"""
당신은 친절한 개인 재무 코치입니다. 아래 이번 달 소비 데이터를 바탕으로 분석 리포트를 작성하세요.

[이번 달 수입]
{monthly_income:,}원

[카테고리별 지출]
{dict(totals)}

[총 지출]
{total_spent:,}원

[예산 초과/임박 경고 항목]
{warnings if warnings else "없음"}

다음 내용을 포함해 리포트를 작성하세요:
1. 이번 달 소비 요약 (칭찬 또는 우려 포함)
2. 예산 초과 항목이 있다면 구체적인 절약 팁
3. 수입 대비 권장 비율: 생활비 50% / 저축 30% / 여가 20% 기준으로 현재 상태 평가
4. 다음 달을 위한 저축 목표액 추천
한국어로 친근하고 실용적으로 작성하세요.
"""
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
    )
    return response.choices[0].message.content
```

---

### Step 5 — 전체 실행 흐름

```python
def process_receipt(image_path: str, user_data: dict) -> dict:
    """
    영수증 1장을 처리해 가계부에 저장할 레코드를 반환한다.

    user_data 예시:
    {
        "monthly_income": 3000000,
        "budget_limits": {"식비": 500000, "쇼핑/의류": 200000},
        "transactions": []  # 이번 달 누적 내역
    }
    """
    # Step 1: 텍스트 추출
    ocr_text = extract_text_from_receipt(image_path)
    if not ocr_text:
        return {"error": "영수증 인식 실패. 이미지를 다시 촬영해 주세요."}

    # Step 2: 필드 구조화
    receipt = extract_receipt_fields(ocr_text)

    # Step 3: 카테고리 분류
    receipt["category"] = classify_category(receipt)

    # 누적 내역에 추가
    user_data["transactions"].append(receipt)

    # Step 4: 경고 즉시 체크
    alert = check_immediate_alert(receipt, user_data)

    return {
        "receipt": receipt,
        "alert": alert,
        "message": f"✅ {receipt['store_name']} {receipt['total_amount']:,}원 — {receipt['category']} 기록 완료"
    }

def check_immediate_alert(receipt: dict, user_data: dict) -> str | None:
    """단건 추가 후 해당 카테고리 예산 상태를 즉시 확인한다."""
    from collections import defaultdict
    cat = receipt.get("category")
    limit = user_data["budget_limits"].get(cat)
    if not limit:
        return None

    totals = defaultdict(int)
    for t in user_data["transactions"]:
        totals[t["category"]] += t["total_amount"]

    ratio = totals[cat] / limit
    if ratio >= 1.0:
        return f"🚨 {cat} 예산 초과! 이번 달 {totals[cat]:,}원 사용 (한도: {limit:,}원)"
    elif ratio >= 0.8:
        return f"⚠️  {cat} 예산 80% 도달. 남은 한도: {limit - totals[cat]:,}원"
    return None
```

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
