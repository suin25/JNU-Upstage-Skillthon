# Edge Cases — 영수증 처리 예외 상황 가이드

이 파일은 `ocr_extract.py`와 `analyze.py`가 마주칠 수 있는 예외 상황과
그 처리 방식을 정리한다. 코드를 수정하거나 새 예외를 추가할 때 이 문서를 먼저 확인한다.

---

## 1. 저품질 이미지 (OCR 신뢰도 < 0.7)

**원인**: 흐린 사진, 역광, 구겨진 영수증, 열지 낡은 잉크

**처리 방식**
- `ocr_receipt()`가 `(text, confidence)` 튜플로 반환
- `confidence < 0.7`이면 경고 출력 후 처리는 계속 진행 (중단 아님)
- 결과 레코드에 `"ocr_confidence"` 필드 저장 → 나중에 필터링 가능

**사용자 안내 메시지**
```
⚠️  OCR 신뢰도 낮음 (58%) — 결과가 부정확할 수 있습니다.
   → 밝은 조명에서 영수증이 화면을 꽉 채우도록 다시 촬영해 주세요.
```

**추후 개선 아이디어**: 신뢰도가 너무 낮으면 재촬영 요청 후 재시도 루프 추가

---

## 2. 날짜가 없는 영수증

**원인**: 영수증 상단이 잘린 사진, 날짜 인쇄 누락

**처리 방식**
- `extract_fields()`에서 `date` 필드가 빈 문자열이면 오늘 날짜(`YYYY-MM-DD`)로 대입
- 콘솔에 안내 메시지 출력

```python
if not receipt.get("date"):
    receipt["date"] = datetime.today().strftime("%Y-%m-%d")
```

**주의**: 날짜가 잘못 들어가면 월별 집계가 틀릴 수 있으므로, 이후 수동 수정 기능 추가 권장

---

## 3. 총액이 없는 영수증

**원인**: OCR 누락, 품목만 있고 합계 행이 없는 간이 영수증

**처리 방식**
- `extract_fields()`에서 `total_amount`가 0이거나 누락이면 품목 단가 × 수량 합산으로 보정

```python
receipt["total_amount"] = sum(
    item.get("price", 0) * item.get("quantity", 1)
    for item in receipt.get("items", [])
)
```

**주의**: 할인, 포인트 적립, 부가세가 있는 경우 합산액이 실제 결제액과 다를 수 있음

---

## 4. 품목이 없는 영수증 (간이 영수증 / 바코드 기반)

**원인**: 편의점 셀프 계산, QR 결제 영수증, 단순 금액만 적힌 영수증

**처리 방식**
- `items` 배열이 비어있어도 저장 허용
- `classify_category()`는 `store_name`만으로 카테고리 분류 시도
- 가게명도 없으면 `"기타"`로 폴백

---

## 5. 외국어 영수증 (영어 / 일본어 등)

**원인**: 해외 여행, 면세점, 외국계 프랜차이즈

**처리 방식**
- Upstage OCR은 영어, 일본어, 한자 등 다국어 지원
- `extract_fields()`의 Information Extract 프롬프트는 언어 무관 JSON 추출 가능
- `classify_category()` Solar LLM 프롬프트는 한국어 카테고리명으로 강제 출력

**한계**: 매우 특수한 언어(아랍어, 태국어 등)는 OCR 정확도 저하 가능

---

## 6. 동일 영수증 중복 등록

**원인**: 같은 이미지를 실수로 두 번 업로드

**현재 처리**: 중복 감지 없음 — 그대로 저장됨

**권장 개선안**:
```python
# 날짜 + 가게명 + 금액 조합으로 중복 체크
def is_duplicate(receipt, transactions):
    for t in transactions:
        if (t.get("date") == receipt.get("date") and
            t.get("store_name") == receipt.get("store_name") and
            t.get("total_amount") == receipt.get("total_amount")):
            return True
    return False
```

---

## 7. 예산 한도가 설정되지 않은 카테고리

**처리 방식**
- `check_budget_alert()`에서 `budget.get(cat)`이 `None`이면 경고 없이 `None` 반환
- 기본 예산은 `config.py`의 `DEFAULT_BUDGET`에 정의되어 있음
- 사용자 커스텀 예산은 `ledger_data.json`의 `"budget"` 키에서 관리

---

## 8. API 호출 실패 / 타임아웃

**원인**: 네트워크 불안정, API 키 만료, 크레딧 부족

**현재 처리**:
- `requests.post(timeout=30)` — 30초 타임아웃
- `response.raise_for_status()` — HTTP 4xx/5xx 즉시 예외

**권장 개선안**: 지수 백오프 재시도 (최대 3회)
```python
import time

for attempt in range(3):
    try:
        response = requests.post(...)
        response.raise_for_status()
        break
    except requests.RequestException as e:
        if attempt == 2:
            raise
        time.sleep(2 ** attempt)
```
