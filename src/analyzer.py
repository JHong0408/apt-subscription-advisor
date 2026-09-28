"""
공고 데이터 + 실거래가 데이터를 조합해 "안전마진"과 "대략적 대출한도"를 계산.

기준: 2026년 9월 시행 규제 (10.15 대책 + 2026 하반기 스트레스 DSR 운영방안)
  - LTV: 규제지역 40% / 비규제지역 70% / 수도권·규제지역 생애최초 70%
  - 수도권·규제지역 1주택자 추가 구입(처분조건 없음) → 주담대 불가(LTV 0%)
  - 주택가격별 총액 한도: 규제지역 15억 이하 6억 / 15~25억 4억 / 25억 초과 2억
                          수도권 비규제지역 6억
  - DSR 40%(은행권), 스트레스 금리: 수도권·규제지역 3.0% / 지방 0.75%(1.5%×50%)
  - 수도권·규제지역 주담대 만기 최장 30년

⚠️ 여전히 근사치입니다. 금리 유형(혼합/주기형)별 스트레스 적용비율, 인정소득,
신용점수, 은행 자체 한도, 잔금대출 경과규정(입주자모집공고일 기준) 등은
반영하지 않았습니다. 최종 판단 전 반드시 집단대출 취급 은행에 확인하세요.

region 판정은 cheongyak_api.classify_region_type()을 사용하세요 - 규제지역 지정
현황(10.15 대책 기준 서울 전역 + 경기 12곳)은 정부가 수시로 재지정하므로, 시간이
지나면 그 목록도 다시 확인이 필요합니다.
"""
from __future__ import annotations

from statistics import mean
from typing import Literal

RegionType = Literal["regulated", "capital_nonregulated", "local"]
HomeStatus = Literal["none", "conditional_disposal", "owner"]

EOK = 100_000_000  # 1억 원


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


def resolve_ltv_ratio(region: RegionType, home_status: HomeStatus,
                      is_first_time_buyer: bool) -> float:
    """지역·보유상태·생애최초 여부로 LTV 비율 결정.

    is_first_time_buyer: 세대원 전원이 주택을 소유한 적이 없는지 여부.
    (청약 '생애최초 특별공급을 썼는지'와는 다른 개념 — 생애최초 특공 당첨자는
     보통 이 우대 대상에 해당합니다.)
    """
    capital_or_regulated = region in ("regulated", "capital_nonregulated")

    if home_status == "owner":
        # 수도권·규제지역: 추가 주택구입 목적 주담대 금지
        return 0.0 if capital_or_regulated else 0.60  # 지방 1주택자는 은행별 상이, 보수적 가정

    # 무주택 또는 처분조건부 1주택 (무주택자와 동일 취급)
    if region == "regulated":
        return 0.70 if (is_first_time_buyer and home_status == "none") else 0.40
    if region == "capital_nonregulated":
        return 0.70
    # 지방 비규제: 생애최초 80%, 일반 70%
    return 0.80 if (is_first_time_buyer and home_status == "none") else 0.70


def price_band_cap(price_krw: int, region: RegionType) -> int | None:
    """주택가격별 주담대 총액 한도. None이면 별도 상한 없음."""
    if region == "regulated":
        if price_krw <= 15 * EOK:
            return 6 * EOK
        if price_krw <= 25 * EOK:
            return 4 * EOK
        return 2 * EOK
    if region == "capital_nonregulated":
        return 6 * EOK
    return None


def default_stress_rate(region: RegionType) -> float:
    """변동금리 기준 스트레스 가산금리 (2026 하반기)."""
    return 0.030 if region in ("regulated", "capital_nonregulated") else 0.0075


def dsr_loan_limit(annual_income_krw: int, annual_rate: float, years: int,
                   dsr_ratio: float = 0.40,
                   existing_annual_debt_service_krw: int = 0) -> int:
    """원리금균등상환 역산: 연 상환여력으로 빌릴 수 있는 원금."""
    annual_capacity = annual_income_krw * dsr_ratio - existing_annual_debt_service_krw
    if annual_capacity <= 0:
        return 0
    monthly_payment = annual_capacity / 12
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return int(monthly_payment * n)
    return int(monthly_payment * (1 - (1 + r) ** -n) / r)


def estimate_loan_capacity(price_krw: int, annual_income_krw: int,
                           region: RegionType = "regulated",
                           home_status: HomeStatus = "none",
                           is_first_time_buyer: bool = False,
                           existing_annual_debt_service_krw: int = 0,
                           interest_rate: float = 0.042,
                           stress_rate: float | None = None,
                           maturity_years: int = 30,
                           dsr_ratio: float = 0.40,
                           ltv_ratio: float | None = None) -> dict:
    """LTV / 가격구간 총액한도 / DSR 중 가장 작은 값을 대출가능액으로 사용.

    - interest_rate: 실제 예상 대출금리 (기본 4.2%, 시장 상황에 맞게 조정)
    - stress_rate: None이면 지역별 기본값(변동금리 기준, 가장 보수적)
    - ltv_ratio: 지정하면 자동 판정을 덮어씀
    """
    if ltv_ratio is None:
        ltv_ratio = resolve_ltv_ratio(region, home_status, is_first_time_buyer)
    if stress_rate is None:
        stress_rate = default_stress_rate(region)
    if region in ("regulated", "capital_nonregulated"):
        maturity_years = min(maturity_years, 30)

    ltv_amount = int(price_krw * ltv_ratio)
    cap = price_band_cap(price_krw, region)
    dsr_amount = dsr_loan_limit(
        annual_income_krw,
        annual_rate=interest_rate + stress_rate,
        years=maturity_years,
        dsr_ratio=dsr_ratio,
        existing_annual_debt_service_krw=existing_annual_debt_service_krw,
    )

    candidates = {"LTV": ltv_amount, "DSR": dsr_amount}
    if cap is not None:
        candidates["PRICE_CAP"] = cap

    binding = min(candidates, key=candidates.get)
    loan_capacity = candidates[binding]
    required_cash = max(price_krw - loan_capacity, 0)

    return {
        "ltv_ratio": ltv_ratio,
        "ltv_based_limit": ltv_amount,
        "price_band_cap": cap,
        "dsr_based_limit": dsr_amount,
        "dsr_rate_used": round(interest_rate + stress_rate, 4),
        "binding_constraint": binding,
        "estimated_loan_capacity": loan_capacity,
        "estimated_required_cash": required_cash,
        "disclaimer": "근사치입니다. 잔금대출 실제 한도는 취급 은행 심사 결과를 따릅니다.",
    }


def within_budget(required_cash: int, cash_available: int) -> bool:
    return required_cash <= cash_available
