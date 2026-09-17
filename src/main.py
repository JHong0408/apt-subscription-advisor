"""
매일 실행되는 진입점.

1. 청약홈에서 신규 무순위/임의공급 "공고 개요" 수집 (면적/분양가 없음)
2. 서울/경기(profile.preferences.prefer_regions)로 지역 필터링
3. 공고마다 주택형별 상세(면적/분양가)를 별도 조회해서 "공고+주택형" 단위로 펼침,
   최소 면적 조건은 여기서 적용 (전용면적 미달 타입만 있는 공고는 자동 제외)
4. 이미 알림 보낸 "공고+주택형" 조합은 건너뜀 (seen_notices.json)
5. 국토부 실거래가로 타입별 안전마진 계산
6. 타입별 대략적 DSR/LTV로 필요 현금 계산
7. homedubu.com / mhb-blog.com 두 청약 분석 블로그에 관련 게시글이 있으면
   "참고 자료" 링크로 같이 붙임 (reference_finder.py, 없어도 그냥 생략)
8. 공고 하나당(타입이 몇 개든) Claude 호출 1번 + Slack 메시지 1개로 묶어서 전송
   (타입별로 따로 보내면 공고당 메시지가 여러 개로 쪼개져서 스팸처럼 되는 걸 방지)
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
import reference_finder

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


def group_new_variants_by_notice(variants: list[dict], seen_ids: set[str]) -> dict[str, list[dict]]:
    """아직 알림 안 보낸 variant만 골라서 notice_id 기준으로 묶는다.

    반환값의 각 리스트는 같은 공고(notice_id)의 신규 주택형들이고, 이걸 한데 묶어서
    공고당 Slack 메시지 1개 + Claude 호출 1번으로 처리한다.
    """
    groups: dict[str, list[dict]] = {}
    for v in variants:
        vid = v.get("variant_id")
        if not vid or vid in seen_ids:
            continue
        groups.setdefault(v["notice_id"], []).append(v)
    return groups


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

    new_by_notice = group_new_variants_by_notice(variants, seen_ids)
    total_new_types = sum(len(v) for v in new_by_notice.values())

    print(
        f"[main] 공고 개요 {len(notices)}건 -> 주택형별로 펼친 후보 {len(variants)}건, "
        f"신규 공고 {len(new_by_notice)}건(타입 {total_new_types}개)"
    )

    if not new_by_notice:
        print("[main] 신규 공고 없음, 종료")
        return

    # homedubu.com 참고 자료 검색은 실행당 1번만 인덱스를 만들어서 재사용한다
    # (공고마다 다시 크롤링하면 요청 수가 너무 많아짐 - reference_finder.py 참고).
    # 실패해도(사이트 접속 불가 등) 빈 리스트로 조용히 넘어가고 알림 자체는 계속 나간다.
    try:
        homedubu_index = reference_finder.build_homedubu_index()
    except Exception as e:  # noqa: BLE001
        print(f"[main] homedubu.com 참고 자료 인덱스 생성 실패: {e}")
        homedubu_index = []

    log_lines = []
    for notice_id, type_variants in new_by_notice.items():
        # 공고 하나 안의 타입들을 각각 분석(면적/분양가별로 실거래가 비교가 다르므로)한 뒤,
        # Claude 호출과 Slack 메시지는 공고당 1번으로 묶는다.
        analyzed_types = []
        for v in type_variants:
            margin, loan = analyze_notice(v, profile)
            analyzed_types.append({"variant": v, "margin": margin, "loan": loan})

        try:
            recommendation = claude_advisor.get_recommendation_multi(analyzed_types, profile)
        except Exception as e:  # noqa: BLE001
            recommendation = f"(AI 추천 생성 실패: {e})"

        house_name = type_variants[0].get("house_name")
        try:
            references = reference_finder.find_all_references(house_name, homedubu_index)
        except Exception as e:  # noqa: BLE001
            print(f"[main] 참고 자료 검색 실패({house_name}): {e}")
            references = []

        message = notifier.format_notice_report_multi(analyzed_types, recommendation, references)
        notifier.send_slack_message(message)

        for v in type_variants:
            seen_ids.add(v["variant_id"])

        log_lines.append(json.dumps(
            {
                "notice_id": notice_id,
                "types": [
                    {"notice": a["variant"], "margin": a["margin"], "loan": a["loan"]}
                    for a in analyzed_types
                ],
                "recommendation": recommendation,
                "references": references,
            },
            ensure_ascii=False,
        ))

    save_seen_ids(seen_ids)
    RUN_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"[main] {len(new_by_notice)}건 알림 전송 완료 (타입 {total_new_types}개 포함)")


if __name__ == "__main__":
    main()