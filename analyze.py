"""
analyze.py — 소비 패턴 분석, 예산 경고, 저축 비율 추천

기능:
  1. 단건 추가 후 즉시 예산 경고 (카테고리별 80% / 100% 임계값)
  2. 월별 소비 집계 및 카테고리 분포 출력
  3. Solar LLM 기반 월간 재무 리포트 + 저축 목표 추천

단독 실행:
  python analyze.py --month 2026-05              # 해당 월 집계만 출력
  python analyze.py --report --income 3000000    # Solar LLM 리포트 생성
  python analyze.py --alert --store 스타벅스 --amount 6500 --category 카페/음료
"""

import argparse
from calendar import monthrange
from collections import defaultdict
from datetime import datetime, timedelta
from config import client, DEFAULT_BUDGET, RECOMMENDED_RATIO, load_data

# ── 즉시 경고 ─────────────────────────────────────────────────────────────────

def check_budget_alert(receipt: dict, transactions: list, budget: dict) -> str | None:
    """
    영수증 1건 추가 후, 해당 카테고리의 이번 달 누적 지출을 확인해
    예산 임계값(80% / 100%) 초과 시 경고 메시지를 반환한다.

    Returns:
        경고 문자열 또는 None (정상 범위)
    """
    cat   = receipt.get("category", "기타")
    limit = budget.get(cat)
    if not limit:
        return None

    month = receipt.get("date", "")[:7]
    totals: dict[str, int] = defaultdict(int)
    for t in transactions:
        if t.get("date", "")[:7] == month:
            totals[t["category"]] = totals[t["category"]] + t.get("total_amount", 0)

    spent = totals.get(cat, 0)
    ratio = spent / limit

    if ratio >= 1.0:
        over  = spent - limit
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


# ── 소비 속도 기반 예산 초과 예측 ─────────────────────────────────────────────

def check_pace_alert(transactions: list, month: str, budget: dict) -> list[str]:
    """
    현재 소비 속도(일별 평균)로 월말까지 예산 초과가 예상되는 카테고리를 탐지한다.
    이미 초과했거나 데이터가 없는 카테고리는 건너뛴다.
    """
    year, mon = map(int, month.split("-"))
    days_in_month = monthrange(year, mon)[1]
    today = datetime.today()

    if today.strftime("%Y-%m") == month:
        days_elapsed = max(today.day, 1)
    else:
        days_elapsed = days_in_month

    monthly = [t for t in transactions if t.get("date", "")[:7] == month]
    totals: dict[str, float] = defaultdict(float)
    for t in monthly:
        totals[t["category"]] += t.get("total_amount", 0)

    warnings = []
    for cat, limit in budget.items():
        spent = totals.get(cat, 0)
        if spent == 0 or spent >= limit:
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


# ── 이상 지출 감지 ─────────────────────────────────────────────────────────────

def check_anomaly(receipt: dict, transactions: list) -> str | None:
    """
    같은 카테고리 이번 달 평균 대비 2배 이상 지출이면
    Solar LLM이 한 문장 코멘트를 생성한다.
    직전 거래가 2건 미만이면 비교 불가로 건너뛴다.
    """
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


# ── 월별 집계 ─────────────────────────────────────────────────────────────────

def summarize_month(transactions: list, month: str, budget: dict) -> dict:
    """
    지정 월(YYYY-MM)의 카테고리별 지출을 집계하고
    예산 대비 사용률을 포함한 요약 딕셔너리를 반환한다.
    """
    monthly = [t for t in transactions if t.get("date", "")[:7] == month]
    totals: dict[str, int] = defaultdict(int)
    for t in monthly:
        totals[t["category"]] = totals[t["category"]] + t.get("total_amount", 0)

    summary = {
        "month":       month,
        "total_spent": sum(totals.values()),
        "by_category": {}
    }
    for cat, spent in sorted(totals.items(), key=lambda x: -x[1]):
        limit = budget.get(cat, 0)
        summary["by_category"][cat] = {
            "spent":  spent,
            "limit":  limit,
            "ratio":  round(spent / limit, 2) if limit else None
        }
    return summary


def print_summary(summary: dict) -> None:
    """월별 집계 결과를 콘솔에 보기 좋게 출력한다."""
    print(f"\n{'='*50}")
    print(f"  📊 {summary['month']} 소비 현황")
    print(f"{'='*50}")
    print(f"  총 지출: {summary['total_spent']:,}원\n")

    for cat, info in summary["by_category"].items():
        bar_len  = int((info["ratio"] or 0) * 20)
        bar_len  = min(bar_len, 20)
        bar      = "█" * bar_len + "░" * (20 - bar_len)
        ratio_str = f"{info['ratio']:.0%}" if info["ratio"] is not None else "  - "
        alert_sym = "🚨" if (info["ratio"] or 0) >= 1.0 else ("⚠️ " if (info["ratio"] or 0) >= 0.8 else "  ")
        print(f"  {alert_sym} {cat:<8} [{bar}] {ratio_str}  ({info['spent']:,}원 / {info['limit']:,}원)")
    print()


# ── Solar LLM 월간 리포트 ──────────────────────────────────────────────────────

def generate_report(summary: dict, monthly_income: int) -> str:
    """
    Solar LLM을 활용해 월간 소비 요약을 바탕으로
    재무 리포트와 저축 목표를 생성한다.

    50/30/20 법칙 기준:
      - 생활비 50%: 필수 지출 (식비, 교통, 생활용품, 의료)
      - 저축   30%: 적금, 비상금
      - 여가   20%: 카페, 쇼핑, 문화, 구독
    """
    total_spent  = summary["total_spent"]
    by_cat       = summary["by_category"]
    savings_room = max(monthly_income - total_spent, 0)

    # 예산 위험 항목 추출
    danger_items = [
        f"{cat}: {info['spent']:,}원 (예산의 {info['ratio']:.0%})"
        for cat, info in by_cat.items()
        if (info["ratio"] or 0) >= 0.8
    ]

    recommended_savings = int(monthly_income * RECOMMENDED_RATIO["저축"])

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
    print("  🤖 Solar LLM 리포트 생성 중...")
    response = client.chat.completions.create(
        model="solar-pro",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900
    )
    return response.choices[0].message.content.strip()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="소비 패턴 분석 및 리포트")
    parser.add_argument("--month",   default=datetime.today().strftime("%Y-%m"),
                        help="분석할 월 (YYYY-MM, 기본값: 이번 달)")
    parser.add_argument("--report",  action="store_true", help="Solar LLM 리포트 생성")
    parser.add_argument("--income",  type=int, default=3_000_000,
                        help="월 수입 (원, 기본값: 3,000,000)")
    # 즉시 경고 테스트용
    parser.add_argument("--alert",    action="store_true", help="단건 경고 테스트")
    parser.add_argument("--store",    help="가게명 (--alert 전용)")
    parser.add_argument("--amount",   type=int, help="금액 (--alert 전용)")
    parser.add_argument("--category", help="카테고리 (--alert 전용)")
    args = parser.parse_args()

    data = load_data()

    if args.alert:
        # 단건 경고 테스트
        dummy = {
            "store_name":   args.store    or "테스트 가게",
            "total_amount": args.amount   or 10000,
            "category":     args.category or "기타",
            "date":         datetime.today().strftime("%Y-%m-%d")
        }
        data["transactions"].append(dummy)
        alert = check_budget_alert(dummy, data["transactions"], data["budget"])
        print(alert or "✅ 예산 범위 내 — 경고 없음")
        return

    # 집계
    summary = summarize_month(data["transactions"], args.month, data["budget"])
    if summary["total_spent"] == 0:
        print(f"{args.month}에 기록된 거래가 없습니다.")
        return

    print_summary(summary)

    if args.report:
        report = generate_report(summary, args.income)
        print(f"{'='*50}")
        print("  📋 재무 리포트 (Solar LLM)")
        print(f"{'='*50}")
        print(report)
        print()


if __name__ == "__main__":
    main()
