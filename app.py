"""
app.py — 영수증 가계부 웹 서버 (FastAPI)

실행:
  python3 app.py
  또는
  uvicorn app:app --reload
"""

import sys
import tempfile
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from ocr_extract import process_image
from analyze import check_budget_alert, summarize_month, generate_report, check_pace_alert, check_anomaly
from config import load_data, save_data, DEFAULT_BUDGET, CATEGORIES, ROOT_DIR

app = FastAPI(title="영수증 가계부", version="1.0.0")

@app.on_event("startup")
def backfill_ids():
    data = load_data()
    changed = False
    for t in data["transactions"]:
        if "id" not in t:
            t["id"] = str(uuid.uuid4())
            changed = True
    if changed:
        save_data(data)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def assign_id(receipt: dict) -> dict:
    if "id" not in receipt:
        receipt["id"] = str(uuid.uuid4())
    return receipt


# ── 영수증 업로드 & 처리 ──────────────────────────────────────────────────────

@app.post("/api/receipt")
async def add_receipt(file: UploadFile = File(...), income: int = 3_000_000):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="이미지 파일만 업로드 가능합니다 (jpg/png).")

    suffix = Path(file.filename or "receipt.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        receipt = process_image(tmp_path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"처리 오류: {e}")
    finally:
        os.unlink(tmp_path)

    assign_id(receipt)
    data = load_data()
    data["transactions"].append(receipt)
    save_data(data)

    month = (receipt.get("date") or datetime.today().strftime("%Y-%m-%d"))[:7]
    alert       = check_budget_alert(receipt, data["transactions"], data["budget"])
    pace_alerts = check_pace_alert(data["transactions"], month, data["budget"])
    anomaly     = check_anomaly(receipt, data["transactions"])

    return {
        "receipt": receipt,
        "alert": alert,
        "pace_alerts": pace_alerts,
        "anomaly": anomaly,
        "message": f"{receipt.get('store_name', '알 수 없음')} {receipt.get('total_amount', 0):,.0f}원 — {receipt.get('category', '기타')} 기록 완료",
    }


# ── 월별 집계 ─────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def get_summary(month: Optional[str] = Query(default=None)):
    if not month:
        month = datetime.today().strftime("%Y-%m")
    data = load_data()
    summary = summarize_month(data["transactions"], month, data["budget"])
    summary["pace_alerts"] = check_pace_alert(data["transactions"], month, data["budget"])
    return summary


# ── AI 재무 리포트 ────────────────────────────────────────────────────────────

class ReportRequest(BaseModel):
    month: Optional[str] = None
    income: int = 3_000_000

@app.post("/api/report")
def get_report(req: ReportRequest):
    month = req.month or datetime.today().strftime("%Y-%m")
    data = load_data()
    summary = summarize_month(data["transactions"], month, data["budget"])
    if summary["total_spent"] == 0:
        raise HTTPException(status_code=404, detail=f"{month}에 기록된 거래가 없습니다.")
    report_text = generate_report(summary, req.income)
    return {"month": month, "report": report_text}


# ── 거래 목록 ─────────────────────────────────────────────────────────────────

@app.get("/api/transactions")
def list_transactions(month: Optional[str] = Query(default=None)):
    data = load_data()
    txns = data["transactions"]
    if month:
        txns = [t for t in txns if t.get("date", "")[:7] == month]
    return {"transactions": txns, "total": len(txns)}


# ── 예산 조회 & 수정 ──────────────────────────────────────────────────────────

@app.get("/api/budget")
def get_budget():
    data = load_data()
    return data["budget"]

@app.put("/api/budget")
def update_budget(budget: dict):
    data = load_data()
    data["budget"].update(budget)
    save_data(data)
    return data["budget"]


# ── 거래 삭제 ────────────────────────────────────────────────────────────────

@app.delete("/api/transactions/{txn_id}")
def delete_transaction(txn_id: str):
    data = load_data()
    before = len(data["transactions"])
    data["transactions"] = [t for t in data["transactions"] if t.get("id") != txn_id]
    if len(data["transactions"]) == before:
        raise HTTPException(status_code=404, detail="거래를 찾을 수 없습니다.")
    save_data(data)
    return {"message": "삭제 완료"}


# ── 데이터 초기화 ─────────────────────────────────────────────────────────────

@app.delete("/api/reset")
def reset_data():
    save_data({"transactions": [], "budget": DEFAULT_BUDGET})
    return {"message": "데이터가 초기화되었습니다."}


# ── 카테고리 목록 ─────────────────────────────────────────────────────────────

@app.get("/api/categories")
def get_categories():
    return CATEGORIES


# ── 수동 지출 입력 ───────────────────────────────────────────────────────────

class ManualEntry(BaseModel):
    store_name: str
    date: Optional[str] = None
    category: str
    total_amount: float
    payment_method: str = "알 수 없음"
    items: list = []

@app.post("/api/entry")
def add_manual_entry(entry: ManualEntry):
    receipt = entry.model_dump()
    if not receipt.get("date"):
        receipt["date"] = datetime.today().strftime("%Y-%m-%d")
    receipt["ocr_confidence"] = 1.0
    assign_id(receipt)

    data = load_data()
    data["transactions"].append(receipt)
    save_data(data)

    month = (receipt.get("date") or datetime.today().strftime("%Y-%m-%d"))[:7]
    alert       = check_budget_alert(receipt, data["transactions"], data["budget"])
    pace_alerts = check_pace_alert(data["transactions"], month, data["budget"])
    anomaly     = check_anomaly(receipt, data["transactions"])

    return {
        "receipt": receipt,
        "alert": alert,
        "pace_alerts": pace_alerts,
        "anomaly": anomaly,
        "message": f"{receipt['store_name']} {receipt['total_amount']:,.0f}원 — {receipt['category']} 기록 완료",
    }


# ── API 키 상태 & 설정 ────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    import config
    return {"api_key_set": bool(config.UPSTAGE_API_KEY)}

class ApiKeyRequest(BaseModel):
    api_key: str

@app.post("/api/setup")
def setup_api_key(req: ApiKeyRequest):
    env_path = ROOT_DIR / ".env"
    env_path.write_text(f"UPSTAGE_API_KEY={req.api_key}\n", encoding="utf-8")
    # 런타임에 즉시 반영
    import config
    import openai
    config.UPSTAGE_API_KEY = req.api_key
    config.client = openai.OpenAI(api_key=req.api_key, base_url="https://api.upstage.ai/v1")
    import ocr_extract, analyze
    ocr_extract.UPSTAGE_API_KEY = req.api_key
    ocr_extract.client = config.client
    return {"message": "API 키가 저장되었습니다."}


# ── 루트 → index.html ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    html_file = static_dir / "index.html"
    if html_file.exists():
        return html_file.read_text(encoding="utf-8")
    return "<h1>static/index.html 파일이 없습니다.</h1>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
