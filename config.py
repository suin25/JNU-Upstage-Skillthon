"""
config.py — 공통 설정, 상수, 클라이언트 초기화

모든 스크립트에서 이 모듈을 import해서 사용한다.
API 키나 예산 기본값을 변경하고 싶을 때 이 파일만 수정하면 된다.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# ── 경로 설정 ──────────────────────────────────────────────────────────────────

ROOT_DIR  = Path(__file__).parent                 # skillthon/
ASSET_DIR = ROOT_DIR
DATA_FILE = ROOT_DIR / "ledger_data.json"         # 영수증 누적 데이터 저장소

# ── API 키 로드 ────────────────────────────────────────────────────────────────

load_dotenv(ROOT_DIR / ".env")
UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY", "")

# ── Upstage 클라이언트 (OpenAI 호환) ──────────────────────────────────────────

client = OpenAI(
    api_key=UPSTAGE_API_KEY or "placeholder",
    base_url="https://api.upstage.ai/v1"
)

# ── 카테고리 정의 ──────────────────────────────────────────────────────────────

CATEGORIES = [
    "식비", "카페/음료", "교통", "쇼핑/의류", "생활용품",
    "의료/건강", "문화/여가", "교육", "구독서비스", "기타"
]

# ── 기본 월 예산 한도 (원) ─────────────────────────────────────────────────────
# 사용자가 ledger_data.json의 "budget" 키를 직접 수정해 개인화할 수 있다.

DEFAULT_BUDGET = {
    "식비":        500_000,
    "카페/음료":   100_000,
    "교통":        150_000,
    "쇼핑/의류":   200_000,
    "생활용품":    100_000,
    "의료/건강":   100_000,
    "문화/여가":   150_000,
    "교육":        100_000,
    "구독서비스":   50_000,
    "기타":        100_000,
}

# ── 저축 권장 비율 (50/30/20 법칙 기준) ───────────────────────────────────────

RECOMMENDED_RATIO = {
    "생활비": 0.50,
    "저축":   0.30,
    "여가":   0.20,
}

# ── 데이터 저장/로드 ───────────────────────────────────────────────────────────

def load_data() -> dict:
    """ledger_data.json에서 누적 데이터를 불러온다. 없으면 초기값 반환."""
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"transactions": [], "budget": DEFAULT_BUDGET}


def save_data(data: dict) -> None:
    """누적 데이터를 ledger_data.json에 저장한다."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
