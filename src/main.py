"""
매일 실행되는 진입점.

1. 청약홈에서 신규 무순위/임의공급 "공고 개요" 수집 (면적/분양가 없음)
2. 서울/경기(profile.preferences.prefer_regions)로 지역 필터링
3. 공고마다 주택형별 상세(면적/분양가)를 별도 조회해서 "공고+주택형" 단위로 펼침,
   최소 면적 조건은 여기서 적용 (전용면적 미달 타입만 있는 공고는 자동 제외)
4. 이미 알림 보낸 "공고+주택형" 조합은 건너뜀 (seen_notices.json)
5. 국토부 실거래가로 안전마진 계산
6. 대략적 DSR/LTV로 필요 현금 계산
7. Claude에게 개인화 추천 요청
8. Slack으로 결과 전송
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import cheongyak_api
import rtms_api
import analyzer
import claude_advisor
import notifier

SEEN_FILE = Path("seen_notices.json")
RUN_LOG_FILE = Path("run_log.jsonl")


def load_profile() -> dict:
    raw = os.environ.get("MY_PROFILE_JSON")
    if not raw:
        raise RuntimeError("환경변수 MY_PROFILE_JSON이 설정되지 않았습니다.")
    return json.loads(raw)


def load_seen_ids() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    return set(json.loads(SEEN_FILE.read_text()))


def save_seen_ids(ids: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2))


def collect_candidate_notices(prefer_regions: list[str] | None = None) -> list[dict]:
    """무순위/임의공급 "공고 개요"를 모아 관심 지역(서울/경기)으로 거른다.

    면적 조건은 여기서 적용하지 않는다 - 공고 개요 엔드포인트에는 면적이 없고,
    실제 면적은 expand_with_house_types()가 주택형별 상세를 조회해야 알 수 있다.

    prefer_regions: profile["preferences"]["prefer_regions"] 값을 그대로 전달.
    예: ["서울"], ["경기"], ["서울", "경기"]. 비어있으면 서울+경기 전체 허용.
    """
    candidates: list[dict] = []

    for endpoint_key in ("apt_remainder", "arbitrary_supply"):
        try:
            raw_list = cheongyak_api.fetch_notices(endpoint_key)
        except Exception as e:  # noqa: BLE001 - 개인용 배치라 단순 로깅 후 계속 진행
            print(f"[main] {endpoint_key} 조회 실패, 건너뜀: {e}")
            continue

        for raw in raw_list:
            notice = cheongyak_api.parse_notice(raw)
            notice["_endpoint_key"] = endpoint_key  # 주택형 상세 조회 시 어느 Mdl 엔드포인트를 쓸지 기억
            if not cheongyak_api.is_target_region(notice.get("address", ""), prefer_regions):
                continue
            candidates.append(notice)

    return candidates


def expand_with_house_types(notice: dict, min_area_sqm: float) -> list[dict]:
    """공고 하나를 "공고+주택형(면적/분양가)" 단위로 펼친다.

    - 주택형 상세 조회가 성공하면: min_area_sqm 이상인 타입만 남긴다.
      (예: 39㎡ 초소형만 있는 공고는 빈 리스트 반환 -> 자동 제외)
    - 주택형 상세 조회 자체가 실패하거나 데이터가 아직 없으면: 면적/분양가 없이
      원본 공고 그대로 1건만 반환해서, 최소한 "이런 공고가 떴다"는 알림은 가도록 한다.

    반환되는 각 dict는 notice의 사본 + house_ty/area_sqm/price_manwon/variant_id.
    variant_id는 seen_notices.json 중복 방지 키로 쓰인다 (공고번호:주택형).
    """
    endpoint_key = notice.get("_endpoint_key")
    pblanc_no = notice.get("notice_id")

    raw_models: list[dict] = []
    if endpoint_key and pblanc_no:
        try:
            raw_models = cheongyak_api.fetch_models(endpoint_key, pblanc_no)
        except Exception as e:  # noqa: BLE001
            print(f"[main] 주택형 상세 조회 실패({pblanc_no}): {e}")

    if not raw_models:
        fallback = dict(notice)
        fallback["variant_id"] = f"{pblanc_no}:unknown"
        return [fallback]

    variants: list[dict] = []
    for raw in raw_models:
        model = cheongyak_api.parse_house_type(raw)
        area = model.get("area_sqm")
        if area is not None and area < min_area_sqm:
            continue
        variant = dict(notice)
        variant["house_ty"] = model.get("house_ty")
        variant["area_sqm"] = area
        variant["price_manwon"] = model.get("price_manwon")
        variant["variant_id"] = f"{pblanc_no}:{model.get('house_ty') or 'unknown'}"
        variants.append(variant)

    return variants


def analyze_notice(notice: dict, profile: dict) -> tuple[dict, dict]:
    region = cheongyak_api.extract_region(notice.get("address", ""))
    area = notice.get("area_sqm")
    price_manwon = _to_int(notice.get("price_manwon"))
    # 만원 단위 → 원 단위로 변환 (RTMS 실거래가와 단위를 맞추기 위함)
    price_krw = price_manwon * 10_000 if price_manwon is not None else 0

    comparable_trades: list[dict] = []
    if region and area:
        try:
            comparable_trades = rtms_api.find_comparable_trades(region, float(area))
        except Exception as e:  # noqa: BLE001
            print(f"[main] 실거래가 조회 실패({region}): {e}")

    margin = analyzer.compute_safety_margin(price_krw, comparable_trades)

    is_homeowner = profile.get("subscription_status", {}).get("is_homeowner", False)
    ltv = 0.70 if not is_homeowner and not profile["subscription_status"].get(
        "used_first_time_buyer_benefit") else 0.40

    loan = analyzer.estimate_loan_capacity(
        price_krw=price_krw,
        annual_income_krw=profile.get("income", {}).get("annual_salary_krw", 0),
        ltv_ratio=ltv,
    )
    return margin, loan


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = str(value).replace(",", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


def main() -> None:
    profile = load_profile()
    seen_ids = load_seen_ids()
    min_area = profile.get("preferences", {}).get("min_area_sqm", 46)
    prefer_regions = profile.get("preferences", {}).get("prefer_regions", ["서울", "경기"])

    notices = collect_candidate_notices(prefer_regions)

    variants: list[dict] = []
    for notice in notices:
        variants.extend(expand_with_house_types(notice, min_area))

    new_variants = [
        v for v in variants
        if v.get("variant_id") and v["variant_id"] not in seen_ids
    ]

    print(f"[main] 공고 개요 {len(notices)}건 -> 주택형별로 펼친 후보 {len(variants)}건, 신규 {len(new_variants)}건")

    if not new_variants:
        print("[main] 신규 공고 없음, 종료")
        return

    log_lines = []
    for notice in new_variants:
        margin, loan = analyze_notice(notice, profile)
        try:
            recommendation = claude_advisor.get_recommendation(notice, margin, loan, profile)
        except Exception as e:  # noqa: BLE001
            recommendation = f"(AI 추천 생성 실패: {e})"

        message = notifier.format_notice_report(notice, margin, loan, recommendation)
        notifier.send_slack_message(message)

        seen_ids.add(notice["variant_id"])
        log_lines.append(json.dumps(
            {"notice": notice, "margin": margin, "loan": loan, "recommendation": recommendation},
            ensure_ascii=False,
        ))

    save_seen_ids(seen_ids)
    RUN_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"[main] {len(new_variants)}건 알림 전송 완료")


if __name__ == "__main__":
    main()