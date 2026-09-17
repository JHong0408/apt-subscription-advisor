"""
매일 실행되는 진입점.

1. 청약홈에서 신규 무순위/임의공급 공고 수집
2. 서울/경기(profile.preferences.prefer_regions) + 최소 면적 조건으로 필터링
3. 이미 알림 보낸 공고는 건너뜀 (seen_notices.json)
4. 국토부 실거래가로 안전마진 계산
5. 대략적 DSR/LTV로 필요 현금 계산
6. Claude에게 개인화 추천 요청
7. Slack으로 결과 전송
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


def collect_candidate_notices(min_area_sqm: float, prefer_regions: list[str] | None = None) -> list[dict]:
    """무순위/임의공급 공고를 모아 관심 지역(서울/경기) + 면적 조건으로 거른다.

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
            if not cheongyak_api.is_target_region(notice.get("address", ""), prefer_regions):
                continue
            area = notice.get("area_sqm")
            try:
                if area is not None and float(area) < min_area_sqm:
                    continue
            except (TypeError, ValueError):
                pass
            candidates.append(notice)

    return candidates


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

    candidates = collect_candidate_notices(min_area, prefer_regions)
    new_candidates = [
        n for n in candidates
        if n.get("notice_id") and n["notice_id"] not in seen_ids
    ]

    print(f"[main] 전체 후보 {len(candidates)}건, 신규 {len(new_candidates)}건")

    if not new_candidates:
        print("[main] 신규 공고 없음, 종료")
        return

    log_lines = []
    for notice in new_candidates:
        margin, loan = analyze_notice(notice, profile)
        try:
            recommendation = claude_advisor.get_recommendation(notice, margin, loan, profile)
        except Exception as e:  # noqa: BLE001
            recommendation = f"(AI 추천 생성 실패: {e})"

        message = notifier.format_notice_report(notice, margin, loan, recommendation)
        notifier.send_slack_message(message)

        seen_ids.add(notice["notice_id"])
        log_lines.append(json.dumps(
            {"notice": notice, "margin": margin, "loan": loan, "recommendation": recommendation},
            ensure_ascii=False,
        ))

    save_seen_ids(seen_ids)
    RUN_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"[main] {len(new_candidates)}건 알림 전송 완료")


if __name__ == "__main__":
    main()
