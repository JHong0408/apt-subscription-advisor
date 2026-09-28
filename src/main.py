"""
매일 실행되는 진입점.

1. 청약홈에서 접수중인 무순위/임의공급 "공고 개요" 수집 (면적/분양가 없음)
2. 서울/경기(profile.preferences.prefer_regions)로 지역 필터링
3. 공고마다 주택형별 상세(면적/분양가)를 별도 조회해서 "공고+주택형" 단위로 펼침,
   최소 면적 조건은 여기서 적용 (전용면적 미달 타입만 있는 공고는 자동 제외)
4. 국토부 실거래가로 타입별 안전마진 계산, 타입별 대략적 DSR/LTV로 필요 현금 계산
   -- 여기까지는 "접수중인 공고 전부"에 대해 매일 다시 계산한다 (Claude 호출이
   아니라 RTMS 조회라 비용 부담이 없고, 사이트에서 항상 최신 안전마진을 보여주기 위함).
5. "공고+주택형" 조합이 seen_notices.json에 이미 있으면(=예전에 한 번 처리한 적
   있으면) Claude 웹검색(homedubu.com/mhb-blog.com 참고자료, reference_finder.py)은
   건너뛴다 - 이 부분만 비용/시간이 크기 때문에 공고당 딱 1번만. AI 추천 문구는 만들지
   않는다 - 참고 URL만 제공한다.
6. apt-advisor 사이트(Cloudflare Worker + D1)로 결과를 전송한다 (site_sync.py).
   신규 처리를 건너뛴 공고는 references를 None으로 보내서, 사이트에 이미 저장된 값을
   덮어쓰지 않고 안전마진/대출한도만 최신화한다. 알림 채널은 Slack이 아니라 이 사이트
   하나뿐이고, 로그인해서 전체 접수중인 공고 검색 + 신규 공고 배지를 확인한다.
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

# 블로그 참고자료 검색(Claude 웹서치) on/off 스위치. 끄려면 False로.
ENABLE_BLOG_SEARCH = True

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

    for endpoint_key in ("apt_general", "apt_remainder", "arbitrary_supply"):
        try:
            raw_list = cheongyak_api.fetch_all_notices(endpoint_key)
        except Exception as e:  # noqa: BLE001 - 개인용 배치라 단순 로깅 후 계속 진행
            print(f"[main] {endpoint_key} 조회 실패, 건너뜀: {e}")
            continue

        raw_total += len(raw_list)
        print(f"[main] {endpoint_key} 원본 {len(raw_list)}건 수신")

        for raw in raw_list:
            notice = cheongyak_api.parse_notice(raw, endpoint_key)
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
        # 청약홈 API가 분양가를 콤마 포함 문자열("66,460")로 줄 때가 있어서, 화면 표시용
        # 값도 analyze_notice()의 계산용 값과 동일하게 여기서 정수로 정리해둔다 (안 그러면
        # 계산은 맞는데 사이트에 보여주는 분양가만 콤마 때문에 NaN으로 깨짐).
        variant["price_manwon"] = _to_int(model.get("price_manwon"))
        variant["variant_id"] = f"{pblanc_no}:{model.get('house_ty') or 'unknown'}"
        variants.append(variant)

    return variants


def group_all_variants_by_notice(variants: list[dict]) -> dict[str, list[dict]]:
    """접수중인 variant 전부를 notice_id 기준으로 묶는다 (신규/기존 구분 없이).

    사이트 검색이 "지금 접수중인 공고 전체"를 보여줘야 하므로, 예전에 이미 처리한
    공고도 매일 다시 묶어서 안전마진/대출한도를 최신화해 사이트로 보낸다. "신규
    여부"(Claude 호출 여부) 판단은 main()에서 variant_id 단위로 seen_ids와 비교해서
    별도로 한다.
    """
    groups: dict[str, list[dict]] = {}
    for v in variants:
        vid = v.get("variant_id")
        if not vid:
            continue
        groups.setdefault(v["notice_id"], []).append(v)
    return groups


def analyze_notice(notice: dict, profile: dict) -> tuple[dict, dict]:
    address = notice.get("address", "")
    region = cheongyak_api.extract_region(address)
    region_type = cheongyak_api.classify_region_type(address)
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

    subscription_status = profile.get("subscription_status", {})
    preferences = profile.get("preferences", {})

    loan = analyzer.estimate_loan_capacity(
        price_krw=price_krw,
        annual_income_krw=profile.get("income", {}).get("annual_salary_krw", 0),
        region=region_type,
        home_status=subscription_status.get("home_status", "none"),
        is_first_time_buyer=subscription_status.get("is_first_time_buyer", False),
        existing_annual_debt_service_krw=profile.get("income", {}).get(
            "existing_annual_debt_service_krw", 0),
        interest_rate=preferences.get("assumed_interest_rate", 0.042),
    )
    return margin, loan


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = str(value).replace(",", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


MHB_DOMAIN = "mhb-blog.com"
HOMEDUBU_DOMAIN = "homedubu.com"


def _ref_seen_key(domain: str, notice_id: str) -> str:
    """블로그 참고자료 검색의 "도메인별" 완료 여부를 seen_ids에 기록하는 키.

    variant_id(공고+주택형)와는 별개 개념 - 참고자료는 단지(공고) 단위지 주택형
    단위가 아니고, "성공(글 찾음 또는 없음 최종 확정)"과 "실패(재시도 필요)"를
    도메인별로 독립적으로 추적해야 한 쪽만 실패했을 때 그 쪽만 다시 시도할 수 있다.
    """
    return f"ref:{domain}:{notice_id}"


def _ref_notfound_once_key(domain: str, notice_id: str) -> str:
    """"없음"이 처음 한 번 나왔음을 기록하는 임시 키 (아직 최종 확정 아님).

    웹검색은 최대 3번의 시도 안에서의 최선의 판단이라(인터넷 전체를 뒤진 게
    아님), 한 번의 "없음"만으로 영구 확정하면 실제로 있는 글을 놓칠 수 있다.
    그래서 "없음"이 두 번 연속 나와야만 최종 확정하고, 그전까지는 다음 실행에서
    한 번 더 재시도한다.
    """
    return f"ref-notfound-once:{domain}:{notice_id}"


def _record_ref_result(domain: str, notice_id: str, result: dict | None, seen_ids: set[str]) -> None:
    key = _ref_seen_key(domain, notice_id)
    if result is not None:
        seen_ids.add(key)  # 글을 찾음 - 바로 최종 확정
        return

    once_key = _ref_notfound_once_key(domain, notice_id)
    if once_key in seen_ids:
        seen_ids.add(key)  # "없음"이 두 번째로도 나옴 - 이제 최종 확정
    else:
        seen_ids.add(once_key)  # 첫 "없음" - 아직 확정하지 않고 재시도 대상으로 남김


def fetch_blog_references(house_name: str, notice_id: str, seen_ids: set[str]) -> tuple[dict | None, dict | None]:
    """mhb-blog/homedubu 참고자료를 동시에 검색해서 (mhb_reference, homedubu_reference)로 반환.

    이미 최종 확정(글을 찾았거나, "없음"이 두 번 연속 나옴)된 도메인은 seen_ids에
    ref:{domain}:{notice_id}로 기록돼 있어서 건너뛴다. 타임아웃/API 오류로 실패했거나
    "없음"이 처음 한 번만 나온 도메인은 최종 확정하지 않으므로 다음 실행에서 그
    도메인만 다시 시도된다 - 이미 확정된 다른 도메인 참고자료를 헛되이 다시 검색하며
    비용을 낭비하지 않는다 (자세한 확정 규칙은 _record_ref_result 참고).

    둘 다 Claude API에 웹검색 포함 요청을 보내는데(최악의 경우 각각 최대 55초),
    순차로 하면 공고 하나당 최대 110초까지 걸려서 job 타임아웃 위험이 커진다.
    서로 완전히 독립적인 조회라 동시에 실행해서 대기 시간을 절반(최대 55초)으로 줄인다.
    """
    mhb_key = _ref_seen_key(MHB_DOMAIN, notice_id)
    homedubu_key = _ref_seen_key(HOMEDUBU_DOMAIN, notice_id)

    mhb_reference: dict | None = None
    homedubu_reference: dict | None = None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if mhb_key not in seen_ids:
            futures["mhb"] = executor.submit(claude_advisor.find_mhb_blog_reference, house_name)
        if homedubu_key not in seen_ids:
            futures["homedubu"] = executor.submit(claude_advisor.find_homedubu_reference, house_name)

        if "mhb" in futures:
            try:
                mhb_reference = futures["mhb"].result()
                _record_ref_result(MHB_DOMAIN, notice_id, mhb_reference, seen_ids)
            except Exception as e:  # noqa: BLE001
                print(f"[main] mhb-blog 참고 자료 검색 실패({house_name}): {e} - 다음 실행에서 재시도")

        if "homedubu" in futures:
            try:
                homedubu_reference = futures["homedubu"].result()
                _record_ref_result(HOMEDUBU_DOMAIN, notice_id, homedubu_reference, seen_ids)
            except Exception as e:  # noqa: BLE001
                print(f"[main] homedubu 참고 자료 검색 실패({house_name}): {e} - 다음 실행에서 재시도")

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

    all_by_notice = group_all_variants_by_notice(variants)

    print(
        f"[main] 공고 개요 {len(notices)}건 -> 주택형별로 펼친 후보 {len(variants)}건, "
        f"접수중인 공고 {len(all_by_notice)}건"
    )

    if not all_by_notice:
        print("[main] 접수중인 후보 공고 없음, 종료")
        return

    log_lines = []
    synced_count = 0
    new_notice_count = 0
    for notice_id, type_variants in all_by_notice.items():
        # 공고 하나 안의 타입들을 각각 분석(면적/분양가별로 실거래가 비교가 다르므로)한다.
        # 이건 매일 다시 계산한다 - RTMS 조회라 비용 부담이 없고, 사이트에 항상 최신
        # 안전마진/대출한도를 반영하기 위함.
        analyzed_types = []
        for v in type_variants:
            margin, loan = analyze_notice(v, profile)
            analyzed_types.append({"variant": v, "margin": margin, "loan": loan})

        new_variant_ids = [v["variant_id"] for v in type_variants if v["variant_id"] not in seen_ids]
        is_new_notice = bool(new_variant_ids)
        # 참고자료 검색은 "주택형"이 아니라 "공고(단지)" 단위 + 도메인별로 완료 여부를 본다 -
        # 지난번에 mhb-blog는 성공하고 homedubu만 타임아웃 났으면, 이번엔 homedubu만 재시도한다.
        refs_incomplete = (
            _ref_seen_key(MHB_DOMAIN, notice_id) not in seen_ids
            or _ref_seen_key(HOMEDUBU_DOMAIN, notice_id) not in seen_ids
        )

        if is_new_notice:
            new_notice_count += 1

        if ENABLE_BLOG_SEARCH and (is_new_notice or refs_incomplete):
            # Claude 호출(블로그 웹검색)은 비용/시간이 커서 도메인당 성공할 때까지만 재시도한다.
            # AI 추천 문구는 만들지 않기로 함 - 참고 URL만 제공한다.
            house_name = type_variants[0].get("house_name")
            mhb_reference, homedubu_reference = fetch_blog_references(house_name, notice_id, seen_ids)
            references = reference_finder.find_all_references(mhb_reference, homedubu_reference)
            # references가 빈 리스트([])여도 그대로 보낸다 - apt-advisor 쪽이 기존 값과
            # 병합(merge)하므로, 이번에 새로 못 찾은 도메인이 있어도 예전에 이미 찾아둔
            # 다른 도메인 참고자료를 지우지 않는다.
        else:
            # 두 도메인 다 이미 완료된 공고이거나, ENABLE_BLOG_SEARCH가 꺼져있는 경우 -
            # 참고자료 관련 호출 자체를 안 함. 꺼져있는 동안은 seen_ids의 ref: 키도 안 건드리므로,
            # 나중에 다시 켜면 그때부터 정상적으로 검색을 재개한다.
            references = None

        # 동기화가 실패해도(사이트 다운 등) 아래에서 seen_ids에는 그대로 추가한다 - 일시적
        # 전송 실패로 같은 공고를 매일 Claude로 재처리하며 비용을 낭비하지 않기 위함.
        if site_sync.sync_notice(notice_id, analyzed_types, references):
            synced_count += 1

        for v in type_variants:
            seen_ids.add(v["variant_id"])

        log_lines.append(json.dumps(
            {
                "notice_id": notice_id,
                "is_new": is_new_notice,
                "types": [
                    {"notice": a["variant"], "margin": a["margin"], "loan": a["loan"]}
                    for a in analyzed_types
                ],
                "references": references,
            },
            ensure_ascii=False,
        ))

    save_seen_ids(seen_ids)
    RUN_LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")
    print(
        f"[main] 접수중 공고 {len(all_by_notice)}건 분석 완료(신규 {new_notice_count}건), "
        f"사이트 동기화 {synced_count}건"
    )


if __name__ == "__main__":
    main()