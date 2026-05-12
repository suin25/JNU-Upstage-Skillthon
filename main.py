"""
main.py — 영수증 가계부 자동화 메인 진입점

ocr_extract.py와 analyze.py를 연결해 전체 파이프라인을 실행한다.

사용법:
  python main.py add    --image receipt.jpg              # 영수증 추가
  python main.py report --income 3000000                 # 이번 달 리포트
  python main.py summary --month 2026-05                 # 월별 집계만
  python main.py reset                                   # 데이터 초기화
"""

import argparse
import sys
from datetime import datetime

from config import load_data, save_data
from ocr_extract import process_image
from analyze import check_budget_alert, summarize_month, print_summary, generate_report


# ── 영수증 추가 ────────────────────────────────────────────────────────────────

def cmd_add(image_path: str, income: int) -> None:
    """영수증 1장을 처리해 가계부에 추가하고 즉시 예산 경고를 출력한다."""
    data = load_data()

    print(f"\n[ 영수증 처리 시작 ]")
    try:
        receipt = process_image(image_path)
    except Exception as e:
        print(f"\n❌ 오류: {e}")
        sys.exit(1)

    data["transactions"].append(receipt)
    save_data(data)

    # 결과 출력
    print(f"\n{'─'*45}")
    print(f"  ✅ 기록 완료")
    print(f"  가게명  : {receipt.get('store_name', '알 수 없음')}")
    print(f"  금액    : {receipt.get('total_amount', 0):,.0f}원")
    print(f"  카테고리: {receipt.get('category', '기타')}")
    print(f"  날짜    : {receipt.get('date', '-')}")
    print(f"  결제    : {receipt.get('payment_method', '알 수 없음')}")
    if receipt.get("ocr_confidence", 1.0) < 0.7:
        print(f"  ⚠️  OCR 신뢰도: {receipt['ocr_confidence']:.0%} (낮음)")
    print(f"{'─'*45}")

    # 즉시 예산 경고
    alert = check_budget_alert(receipt, data["transactions"], data["budget"])
    if alert:
        print(f"\n{alert}\n")
    else:
        print()


# ── 월별 집계 ─────────────────────────────────────────────────────────────────

def cmd_summary(month: str) -> None:
    """지정 월의 카테고리별 지출을 집계해 출력한다."""
    data    = load_data()
    summary = summarize_month(data["transactions"], month, data["budget"])
    if summary["total_spent"] == 0:
        print(f"\n{month}에 기록된 거래가 없습니다.")
        print("  → python main.py add --image receipt.jpg 로 영수증을 추가하세요.")
        return
    print_summary(summary)


# ── 월간 리포트 ───────────────────────────────────────────────────────────────

def cmd_report(month: str, income: int) -> None:
    """집계 + Solar LLM 재무 리포트를 생성한다."""
    data    = load_data()
    summary = summarize_month(data["transactions"], month, data["budget"])
    if summary["total_spent"] == 0:
        print(f"\n{month}에 기록된 거래가 없습니다.")
        return

    print_summary(summary)
    report = generate_report(summary, income)
    print(f"{'='*50}")
    print(f"  📋 재무 리포트 (Solar LLM)")
    print(f"{'='*50}")
    print(report)
    print()


# ── 데이터 초기화 ─────────────────────────────────────────────────────────────

def cmd_reset() -> None:
    """ledger_data.json을 초기화한다. (복구 불가)"""
    confirm = input("⚠️  모든 거래 데이터를 삭제합니다. 계속하시겠습니까? (yes/no): ")
    if confirm.strip().lower() == "yes":
        from config import DEFAULT_BUDGET
        save_data({"transactions": [], "budget": DEFAULT_BUDGET})
        print("✅ 데이터가 초기화되었습니다.")
    else:
        print("취소되었습니다.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="영수증 가계부 자동화 — Upstage API 활용",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py add     --image receipt.jpg
  python main.py add     --image receipt.jpg --income 4000000
  python main.py summary --month 2026-05
  python main.py report  --income 3000000
  python main.py report  --month 2026-04 --income 3000000
  python main.py reset
        """
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # add
    p_add = sub.add_parser("add", help="영수증 이미지를 추가한다")
    p_add.add_argument("--image",  required=True, help="영수증 이미지 경로 (jpg/png)")
    p_add.add_argument("--income", type=int, default=3_000_000, help="월 수입 (원)")

    # summary
    p_sum = sub.add_parser("summary", help="월별 카테고리 집계를 출력한다")
    p_sum.add_argument("--month", default=datetime.today().strftime("%Y-%m"),
                       help="분석할 월 (YYYY-MM)")

    # report
    p_rep = sub.add_parser("report", help="Solar LLM 월간 재무 리포트를 생성한다")
    p_rep.add_argument("--month",  default=datetime.today().strftime("%Y-%m"),
                       help="분석할 월 (YYYY-MM)")
    p_rep.add_argument("--income", type=int, default=3_000_000, help="월 수입 (원)")

    # reset
    sub.add_parser("reset", help="모든 거래 데이터를 초기화한다")

    args = parser.parse_args()

    if   args.cmd == "add":     cmd_add(args.image, args.income)
    elif args.cmd == "summary": cmd_summary(args.month)
    elif args.cmd == "report":  cmd_report(args.month, args.income)
    elif args.cmd == "reset":   cmd_reset()


if __name__ == "__main__":
    main()
