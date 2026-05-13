"""
ocr_extract.py — 영수증 이미지에서 텍스트 추출 및 필드 구조화

파이프라인:
  영수증 이미지 → [Upstage OCR] → 원시 텍스트
               → [Upstage Information Extract] → 구조화된 JSON

단독 실행:
  python ocr_extract.py --image ./receipt.jpg
  python ocr_extract.py --image ./receipt.jpg --save   # ledger_data.json에 바로 저장
"""

import json
import base64
import argparse
import requests
from datetime import datetime
from openai import OpenAI
from config import UPSTAGE_API_KEY, client, CATEGORIES, load_data, save_data

ie_client = OpenAI(
    api_key=UPSTAGE_API_KEY or "placeholder",
    base_url="https://api.upstage.ai/v1/information-extraction"
)

# ── Information Extract 스키마 ─────────────────────────────────────────────────
# 영수증에서 뽑아낼 필드를 명시적으로 정의한다.
# 스키마가 명확할수록 LLM이 누락·오류 없이 구조화한다.

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name": {
            "type": "string",
            "description": "가게 또는 상호명"
        },
        "date": {
            "type": "string",
            "description": "구매 날짜 (YYYY-MM-DD). 영수증에 없으면 빈 문자열."
        },
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
        "total_amount": {
            "type": "number",
            "description": "총 결제 금액 (원). 영수증의 '합계' 또는 '결제금액' 기준."
        },
        "payment_method": {
            "type": "string",
            "description": "결제 수단 (카드 / 현금 / 간편결제 / 알 수 없음)"
        }
    }
}

# ── Step 1: OCR ────────────────────────────────────────────────────────────────

def ocr_receipt(image_path: str) -> tuple[str, float]:
    """
    Upstage Document OCR로 영수증 이미지에서 텍스트를 추출한다.

    Returns:
        (추출된 텍스트, OCR 신뢰도 0~1)

    Raises:
        ValueError: 이미지에서 텍스트를 전혀 인식하지 못한 경우
        requests.HTTPError: API 호출 실패
    """
    print(f"  📷 OCR 처리 중: {image_path}")
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


# ── Step 2: Information Extract ────────────────────────────────────────────────

def extract_fields(image_path: str) -> dict:
    """
    Upstage Information Extract로 영수증 이미지를 구조화된 JSON으로 변환한다.

    날짜 누락 → 오늘 날짜 자동 대입
    총액 누락 → 품목 단가 × 수량 합산으로 보정
    """
    print("  🔍 필드 추출 중 (Information Extract)...")
    with open(image_path, "rb") as f:
        ext = image_path.rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
        b64 = base64.b64encode(f.read()).decode()

    response = ie_client.chat.completions.create(
        model="information-extract",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        ]}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "receipt_schema",
                "schema": RECEIPT_SCHEMA
            }
        }
    )
    receipt: dict = json.loads(response.choices[0].message.content)

    # 날짜 폴백
    if not receipt.get("date"):
        receipt["date"] = datetime.today().strftime("%Y-%m-%d")
        print(f"  📅 날짜 미인식 → 오늘({receipt['date']})로 설정")

    # 총액 폴백
    if not receipt.get("total_amount"):
        receipt["total_amount"] = sum(
            item.get("price", 0) * item.get("quantity", 1)
            for item in receipt.get("items", [])
        )
        print("  💰 총액 미인식 → 품목 합산으로 계산")

    return receipt


# ── Step 3: 카테고리 분류 ──────────────────────────────────────────────────────

def classify_category(receipt: dict) -> str:
    """
    Solar LLM으로 가게명과 품목을 보고 소비 카테고리를 분류한다.
    인식 불가 시 "기타"로 폴백한다.
    """
    print("  🏷️  카테고리 분류 중 (Solar LLM)...")
    item_names = [item.get("name", "") for item in receipt.get("items", [])]
    prompt = (
        f"다음 영수증 정보를 보고 아래 카테고리 중 하나로만 답하시오. 카테고리명만 출력.\n"
        f"카테고리: {', '.join(CATEGORIES)}\n\n"
        f"분류 기준:\n"
        f"- 식비: 식당, 배달, 편의점 소액 구매 등 즉시 먹을 목적의 지출\n"
        f"- 생활용품: 대형마트·창고형 매장(이마트, 코스트코, 홈플러스 등) 또는 "
        f"식재료·생필품을 여러 번 나눠 쓸 목적으로 대량 구매한 경우\n"
        f"- 카페/음료: 커피숍, 음료 전문점\n\n"
        f"가게명: {receipt.get('store_name', '알 수 없음')}\n"
        f"금액: {receipt.get('total_amount', 0):,.0f}원\n"
        f"품목: {item_names if item_names else '정보 없음'}"
    )
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20
    )
    raw = response.choices[0].message.content.strip()
    return raw if raw in CATEGORIES else "기타"


# ── 전체 파이프라인 ────────────────────────────────────────────────────────────

def process_image(image_path: str) -> dict:
    """
    이미지 경로를 받아 OCR → 필드 추출 → 카테고리 분류까지 실행하고
    완성된 영수증 레코드를 반환한다.
    """
    text, confidence = ocr_receipt(image_path)

    if confidence < 0.7:
        print(f"  ⚠️  OCR 신뢰도 낮음 ({confidence:.0%}) — 결과가 부정확할 수 있습니다.")
        print("     → ../references/edge_cases.md 의 '저품질 이미지' 섹션 참고")

    receipt = extract_fields(image_path)
    receipt["category"] = classify_category(receipt)
    receipt["ocr_confidence"] = round(confidence, 3)

    return receipt


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="영수증 OCR + 필드 추출")
    parser.add_argument("--image", required=True, help="영수증 이미지 경로 (jpg/png)")
    parser.add_argument("--save",  action="store_true", help="결과를 ledger_data.json에 저장")
    args = parser.parse_args()

    print(f"\n[ 영수증 처리 시작 ]")
    receipt = process_image(args.image)

    print(f"\n[ 추출 결과 ]")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))

    if args.save:
        data = load_data()
        data["transactions"].append(receipt)
        save_data(data)
        print(f"\n✅ ledger_data.json에 저장 완료")


if __name__ == "__main__":
    main()
