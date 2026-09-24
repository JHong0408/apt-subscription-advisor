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
8. 공고 하나당(타입이 몇 개든) Claude 호출 1번으로 추천 문구를 만들고, apt-advisor
   사이트(Cloudflare Worker + D1)로 결과를 전송 (site_sync.py). 알림 채널은 Slack이
   아니라 이 사이트 하나뿐이고, 로그인해서 전체 공고 검색 + 신규 공고 배지를 확인한다.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import cheongyak_api
import rtms_api
import analyzer
import claude_advisor
import site_sync
import reference_finder

SEEN_FILE = Path("seen_notices.json")
RUN_LOG_FILE = Path("run_log.jsonl")

KST = timezone(timedelta(hours=9))


def today_kst() -> date:
    """GitHub Actions 러너는 UTC로 도니까, 마감일 비교는 반드시 KST 기준으로 해야 한다."""
    return datetime.now(KST).date()


def is_reception_open(notice: dict, today: date) -> bool:
    """접수 종료일(reception_end_date)이 이미 지난 공고는 걸러낸다.

    종료일 정보가 없거나 형식이 이상하면(파싱 실패) 임의로 걸러내지 않고 일단
    통과시킨다 - "모른다"를 "마감됐다"로 잘못 단정하지 않기 위함.
    """
    end_date_str = notice.get("reception_end_date")
    if not end_date_str:
        return True
    try:
        end_date = date.fromisoformat(end_date_str)
    except ValueError:
        return True
    return end_date >= today


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

    접수 마감일이 이미 지난 공고도 여기서 걸러낸다 - seen_notices.json에 기록이
    안 남아있으면(예: 이전 실행이 도중에 죽어서 저장을 못한 경우) 마감이 한참
    지난 공고도 "신규"로 잡혀서 계속 알림이 갈 수 있기 때문.

    prefer_regions: profile["preferences"]["prefer_regions"] 값을 그대로 전달.
    예: ["서울"], ["경기"], ["서울", "경기"]. 비어있으면 서울+경기 전체 허용.
    """
    candidates: list[dict] = []
    today = today_kst()
    expired_count = 0
    out_of_region_count = 0
    raw_total = 0

    for endpoint_key in ("apt_remainder", "arbitrary_supply"):
        try:
            raw_list = cheongyak_api.fetch_all_notices(endpoint_key)
        except Exception as e:  # noqa: BLE001 - 개인용 배치라 단순 로깅 후 계속 진행
            print(f"[main] {endpoint_key} 조회 실패, 건너뜀: {e}")
            continue

        raw_total += len(raw_list)
        print(f"[main] {endpoint_key} 원본 {len(raw_list)}건 수신")

        for raw in raw_list:
            notice = cheongyak_api.parse_notice(raw)
            notice["_endpoint_key"] = endpoint_key  # 주택형 상세 조회 시 어느 Mdl 엔드포인트를 쓸지 기억

            if not cheongyak_api.is_target_region(notice.get("address", ""), prefer_regions):
                out_of_region_count += 1
                continue
            if not is_reception_open(notice, today):
                expired_count += 1
                continue
            candidates.append(notice)

    print(
        f"[main] 원본 총 {raw_total}건 -> 지역 필터 제외 {out_of_region_count}건, "
        f"접수 마감 제외 {expired_count}건, 최종 후보 {len(candidates)}건"
    )

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


def fetch_blog_references(house_name: str) -> tuple[dict | None, dict | None]:
    """mhb-blog/homedubu 참고자료를 동시에 검색해서 (mhb_reference, homedubu_reference)로 반환.

    둘 다 Claude API에 웹검색 포함 요청을 보내는데(최악의 경우 각각 최대 55초),
    순차로 하면 공고 하나당 최대 110초까지 걸려서 job 타임아웃 위험이 커진다.
    서로 완전히 독립적인 조회라 동시에 실행해서 대기 시간을 절반(최대 55초)으로 줄인다.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        mhb_future = executor.submit(claude_advisor.find_mhb_blog_reference, house_name)
        homedubu_future = executor.submit(claude_advisor.find_homedubu_reference, house_name)

        try:
            mhb_reference = mhb_future.result()
        except Exception as e:  # noqa: BLE001
            print(f"[main] mhb-blog 참고 자료 검색 실패({house_name}): {e}")
            mhb_reference = None

        try:
            homedubu_reference = homedubu_future.result()
        except Exception as e:  # noqa: BLE001
            print(f"[main] homedubu 참고 자료 검색 실패({house_name}): {e}")
            homedubu_reference = None

    return mhb_reference, homedubu_reference


def main() -> None:
    profile = load_profile()
    seen_ids = load_seen_ids()
    min_area = profile.get("preferences", {}).get("min_area_sqm", 46)
    prefer_regions = profile.get("preferences", {}).get("prefer_regions", ["서울", "경기"])
    print(f"[main][추적] 실제 사용되는 prefer_regions={prefer_regions!r}")

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

    log_lines = []
    synced_count = 0
    for notice_id, type_variants in new_by_notice.items():
        # 공고 하나 안의 타입들을 각각 분석(면적/분양가별로 실거래가 비교가 다르므로)한 뒤,
        # Claude 호출과 사이트 동기화는 공고당 1번으로 묶는다.
        analyzed_types = []
        for v in type_variants:
            margin, loan = analyze_notice(v, profile)
            analyzed_types.append({"variant": v, "margin": margin, "loan": loan})

        try:
            recommendation = claude_advisor.get_recommendation_multi(analyzed_types, profile)
        except Exception as e:  # noqa: BLE001
            recommendation = f"(AI 추천 생성 실패: {e})"

        house_name = type_variants[0].get("house_name")
        mhb_reference, homedubu_reference = fetch_blog_references(house_name)

        references = reference_finder.find_all_references(mhb_reference, homedubu_reference)

        # 동기화가 실패해도(사이트 다운 등) 아래에서 seen_ids에는 그대로 추가한다 - 원래
        # Slack 전송도 실패 시 콘솔 출력만 하고 넘어갔던 것과 같은 원칙: 일시적 전송 실패로
        # 같은 공고를 매일 재시도하며 스팸처럼 반복 알리지 않는다.
        if site_sync.sync_notice(notice_id, analyzed_types, recommendation, references):
            synced_count += 1

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
    print(f"[main] {len(new_by_notice)}건 분석 완료 (타입 {total_new_types}개 포함), 사이트 동기화 {synced_count}건")


if __name__ == "__main__":
    main()