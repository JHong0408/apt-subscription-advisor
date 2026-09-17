"""
공고 데이터 + 실거래가 데이터를 조합해 "안전마진"과 "대략적 대출한도"를 계산.

⚠️ 여기 DSR 계산은 매우 단순화된 근사치입니다. 실제 은행 심사는
스트레스 금리 단계, 기존 부채, 신용점수, 상품별 조건에 따라 크게 달라지므로
최종 판단 전 반드시 은행/대출상담사를 통해 확인하세요.
"""
from __future__ import annotations

from statistics import mean


def parse_deal_amount(amount_str: str | None) -> int | None:
    """'123,456' 같은 만원 단위 문자열을 원 단위 정수로 변환."""
    if not amount_str:
        return None
    cleaned = amount_str.replace(",", "").strip()
    if not cleaned.isdigit():
        return None
    return int(cleaned) * 10_000


def compute_safety_margin(subscription_price_krw: int, comparable_trades: list[dict]) -> dict:
    """분양가와 인근 실거래가를 비교해 격차를 계산."""
    amounts = [
        parse_deal_amount(t.get("deal_amount_10k_krw"))
        for t in comparable_trades
    ]
    amounts = [a for a in amounts if a is not None]

    if not amounts:
        return {
            "comparable_count": 0,
            "avg_market_price": None,
            "max_market_price": None,
            "margin_vs_avg": None,
            "margin_pct_vs_avg": None,
            "note": "비교 가능한 인근 실거래 데이터가 없습니다.",
        }

    avg_price = round(mean(amounts))
    max_price = max(amounts)
    margin_vs_avg = avg_price - subscription_price_krw  # 양수면 분양가가 저렴, 음수면 비쌈

    return {
        "comparable_count": len(amounts),
        "avg_market_price": avg_price,
        "max_market_price": max_price,
        "margin_vs_avg": margin_vs_avg,
        "margin_pct_vs_avg": round(margin_vs_avg / avg_price * 100, 1) if avg_price else None,
        "note": (
            "분양가가 인근 평균 실거래가보다 저렴함 (양수 = 저렴)"
            if margin_vs_avg >= 0 else
            "분양가가 인근 평균 실거래가보다 비쌈 (음수 = 프리미엄)"
        ),
    }


def estimate_loan_capacity(price_krw: int, annual_income_krw: int,
                            is_regulated_area: bool = True,
                            ltv_ratio: float = 0.40,
                            dsr_ratio: float = 0.40,
                            stress_rate_discount: float = 0.72) -> dict:
    """매우 단순화된 LTV/DSR 근사치 계산.

    - LTV: 규제지역 무주택자 기준 40%를 기본값으로 사용 (생애최초 등 우대는
      호출하는 쪽에서 ltv_ratio를 조정해서 넘기세요, 예: 0.70)
    - DSR: 연소득 * dsr_ratio 를 "연간 원리금 상환 가능액"으로 보고,
      대략적인 원리금균등상환(고정) 가정하에 대출 가능액을 역산.
      stress_rate_discount는 스트레스 DSR 가산으로 인한 한도 축소 비율의
      거친 근사치입니다 (수도권 규제지역 3.0%p 가산 시 대략 0.7배 수준).
    """
    ltv_amount = int(price_krw * ltv_ratio)

    # 아주 단순화한 DSR 역산: 연소득의 dsr_ratio를 연간 상환액으로 보고,
    # "대출액 ≈ 연간상환가능액 × 10" 같은 매우 거친 배수 대신,
    # 실측 사례 비율(참고: 연소득 대비 대출한도 약 5~5.5배 수준)을 사용.
    dsr_amount = int(annual_income_krw * dsr_ratio * 12.5 * stress_rate_discount)

    loan_capacity = min(ltv_amount, dsr_amount)
    required_cash = max(price_krw - loan_capacity, 0)

    return {
        "ltv_based_limit": ltv_amount,
        "dsr_based_limit": dsr_amount,
        "binding_constraint": "LTV" if ltv_amount < dsr_amount else "DSR",
        "estimated_loan_capacity": loan_capacity,
        "estimated_required_cash": required_cash,
        "disclaimer": "매우 거친 근사치입니다. 실제 심사 결과와 다를 수 있습니다.",
    }


def within_budget(required_cash: int, cash_available: int) -> bool:
    return required_cash <= cash_available
