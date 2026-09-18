"""
한국부동산원 청약홈 분양정보 조회 서비스 클라이언트.

데이터 출처: 공공데이터포털 "한국부동산원_청약홈 분양정보 조회 서비스"
             (odcloud 기반, https://api.odcloud.kr)

이 API는 "공고 개요"와 "주택형별 상세(면적/분양가)"를 서로 다른 엔드포인트로
나눠서 제공한다. 실제 운영 중인 공개 구현체(GitHub: Jung-Yunho/cheongyak-alert)로
교차 확인한 값:
- *Detail 엔드포인트: 공고 개요만 반환 (단지명/주소/공급구분 등). 면적·분양가 없음.
- *Mdl 엔드포인트: PBLANC_NO(공고번호)로 필터링해서 주택형별 상세(면적 포함된
  HOUSE_TY 코드, 분양가 LTTOT_TOP_AMOUNT)를 반환.
따라서 공고 하나당 반드시 Detail → Mdl 두 번 호출해야 면적/분양가를 알 수 있다.
"""
from __future__ import annotations

import os
import re
import requests

BASE_URL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"

ENDPOINTS = {
    "apt_general": "/getAPTLttotPblancDetail",       # 일반분양 공고 개요
    "apt_remainder": "/getRemndrLttotPblancDetail",  # 무순위/잔여세대(+불법행위 재공급) 공고 개요
    "arbitrary_supply": "/getOPTLttotPblancDetail",  # 임의공급 공고 개요
}

# 공고 개요 엔드포인트 → 그 공고의 주택형별 상세(면적/분양가) 엔드포인트
MDL_ENDPOINTS = {
    "apt_general": "/getAPTLttotPblancMdl",
    "apt_remainder": "/getRemndrLttotPblancMdl",
    "arbitrary_supply": "/getOPTLttotPblancMdl",
}

# HOUSE_TY 코드(예: "084.7402A")에서 앞자리 숫자(전용면적)만 뽑아내는 패턴
_AREA_RE = re.compile(r"^(\d+(?:\.\d+)?)")

# 서울 25개 자치구 이름으로 대략 매칭할 때 쓰는 키워드 (주소 문자열 매칭용)
SEOUL_GU_LIST = [
    "종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구",
    "성북구", "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구",
    "양천구", "강서구", "구로구", "금천구", "영등포구", "동작구", "관악구",
    "서초구", "강남구", "송파구", "강동구",
]

# 경기도 31개 시/군 이름 (주소 문자열 매칭용). "경기" 접두가 주소에 있는지 먼저
# 확인한 뒤에만 이 목록으로 매칭한다 (예: "광주시"가 "광주광역시"와 헷갈리지 않도록).
GYEONGGI_CITY_LIST = [
    "수원시", "성남시", "의정부시", "안양시", "부천시", "광명시", "평택시",
    "동두천시", "안산시", "고양시", "과천시", "구리시", "남양주시", "오산시",
    "시흥시", "군포시", "의왕시", "하남시", "용인시", "파주시", "이천시",
    "안성시", "김포시", "화성시", "광주시", "양주시", "포천시", "여주시",
    "연천군", "가평군", "양평군",
]

# 구가 나뉘어 있는 6개 경기도 시. 주소에 구 이름까지 있으면 더 좁은 범위로 매칭.
GYEONGGI_DISTRICT_CITIES = {
    "수원시": ["장안구", "권선구", "팔달구", "영통구"],
    "성남시": ["수정구", "중원구", "분당구"],
    "안양시": ["만안구", "동안구"],
    "안산시": ["상록구", "단원구"],
    "고양시": ["덕양구", "일산동구", "일산서구"],
    "용인시": ["처인구", "기흥구", "수지구"],
}


class CheongyakAPIError(RuntimeError):
    pass


def _get_service_key() -> str:
    key = os.environ.get("CHEONGYAK_SERVICE_KEY")
    if not key:
        raise CheongyakAPIError("환경변수 CHEONGYAK_SERVICE_KEY가 설정되지 않았습니다.")
    return key


def fetch_notices(endpoint_key: str, page: int = 1, per_page: int = 100) -> list[dict]:
    """지정한 유형(endpoint_key)의 공고 목록을 가져온다.

    odcloud 계열 API의 공통 파라미터 규칙(page/perPage/serviceKey/returnType)을
    사용합니다. 실제 응답 필드명은 Swagger로 검증 후 parse_notice()에서
    맞춰 파싱하세요.
    """
    if endpoint_key not in ENDPOINTS:
        raise CheongyakAPIError(f"알 수 없는 endpoint_key: {endpoint_key}")

    url = BASE_URL + ENDPOINTS[endpoint_key]
    params = {
        "page": page,
        "perPage": per_page,
        "serviceKey": _get_service_key(),
        "returnType": "JSON",
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # odcloud 표준 응답은 보통 {"data": [...], "currentCount": N, "matchCount": N, "page": N, ...}
    match_count = data.get("matchCount")
    current_count = data.get("currentCount")
    if match_count is not None and match_count > per_page:
        # matchCount(전체 건수)가 한 페이지 분량(per_page)보다 많다는 건 - 우리가
        # page=1만 호출하는 지금 구조로는 뒷페이지 데이터를 놓치고 있다는 뜻.
        print(
            f"[cheongyak_api] {endpoint_key}: 전체 {match_count}건 중 이번 페이지 "
            f"{current_count}건만 받아옴 (page={page}, perPage={per_page}) - 뒷페이지 존재"
        )

    return data.get("data", [])


def fetch_models(endpoint_key: str, pblanc_no: str, page: int = 1, per_page: int = 100) -> list[dict]:
    """해당 공고(PBLANC_NO)의 주택형별 상세(면적/공급세대수/분양가) 목록을 가져온다.

    odcloud API의 필터 문법 cond[FIELD::EQ]=값 을 사용해 PBLANC_NO로 좁힌다.
    """
    if endpoint_key not in MDL_ENDPOINTS:
        raise CheongyakAPIError(f"알 수 없는 endpoint_key: {endpoint_key}")

    url = BASE_URL + MDL_ENDPOINTS[endpoint_key]
    params = {
        "page": page,
        "perPage": per_page,
        "serviceKey": _get_service_key(),
        "returnType": "JSON",
        "cond[PBLANC_NO::EQ]": pblanc_no,
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


def is_seoul(address: str) -> bool:
    return "서울" in (address or "")


def is_gyeonggi(address: str) -> bool:
    return "경기" in (address or "")


def extract_gu(address: str) -> str | None:
    """주소 문자열에서 서울 자치구 이름을 추출. 매칭 실패 시 None."""
    if not address:
        return None
    for gu in SEOUL_GU_LIST:
        if gu in address:
            return gu
    return None


def extract_gyeonggi_region(address: str) -> str | None:
    """주소 문자열에서 경기도 시/군(+구) 이름을 추출.

    구가 있는 시는 "수원시 영통구"처럼 시+구를 합쳐서 반환하고(rtms_api의
    GYEONGGI_LAWD_CD 키와 맞춤), 구가 없으면 시/군 이름만 반환한다.
    매칭 실패 시 None.
    """
    if not address:
        return None
    for city in GYEONGGI_CITY_LIST:
        if city in address:
            districts = GYEONGGI_DISTRICT_CITIES.get(city)
            if districts:
                for d in districts:
                    if d in address:
                        return f"{city} {d}"
            return city
    return None


def extract_region(address: str) -> str | None:
    """서울/경기 구분 없이, rtms_api.ALL_LAWD_CD에 바로 쓸 수 있는 지역 키를 추출."""
    if is_seoul(address):
        return extract_gu(address)
    if is_gyeonggi(address):
        return extract_gyeonggi_region(address)
    return None


def is_target_region(address: str, prefer_regions: list[str] | None = None) -> bool:
    """profile["preferences"]["prefer_regions"]에 맞춰 주소가 관심 지역인지 판단.

    prefer_regions에 "서울"/"서울특별시"가 있으면 서울 주소를, "경기"/"경기도"가
    있으면 경기 주소를 허용. 값이 없거나 인식 못하는 값만 있으면 서울+경기 전체를
    기본값으로 허용한다.
    """
    if not address:
        return False

    prefer_regions = prefer_regions or []
    checks = []
    for region in prefer_regions:
        if region in ("서울", "서울특별시"):
            checks.append(is_seoul(address))
        elif region in ("경기", "경기도"):
            checks.append(is_gyeonggi(address))

    if checks:
        return any(checks)

    # prefer_regions가 비어있거나 인식 불가한 값만 있으면 서울+경기 전체 허용
    return is_seoul(address) or is_gyeonggi(address)


def parse_notice(raw: dict) -> dict:
    """공고 개요(*Detail) API 응답 1건을 파이프라인 공통 포맷으로 변환.

    실제 응답으로 확인된 필드명 기준(2026-09-17 실제 API 호출로 검증):
    HOUSE_NM, HSSPLY_ADRES, HOUSE_SECD_NM(공급구분: 무순위/불법행위 재공급 등),
    RCRIT_PBLANC_DE, PBLANC_NO, PBLANC_URL 등.

    ⚠️ 이 엔드포인트는 면적/분양가를 포함하지 않는다 — 그건 fetch_models()로
    PBLANC_NO를 넘겨 별도 조회해야 한다 (main.py의 expand_with_house_types 참고).
    """
    return {
        "raw": raw,
        "house_name": raw.get("HOUSE_NM"),
        "address": raw.get("HSSPLY_ADRES"),
        "supply_type": raw.get("HOUSE_SECD_NM"),
        "recruit_date": raw.get("RCRIT_PBLANC_DE"),
        "notice_url": raw.get("PBLANC_URL"),
        # 신청 접수 종료일(YYYY-MM-DD) - main.py에서 이미 마감된 공고를 걸러낼 때 씀
        "reception_end_date": raw.get("SUBSCRPT_RCEPT_ENDDE"),
        # 아래 둘은 이 엔드포인트에 없음 - fetch_models()로 채워지기 전까지는 None
        "area_sqm": None,
        "price_manwon": None,
        "notice_id": raw.get("PBLANC_NO"),
    }


def parse_area_from_house_ty(house_ty: str | None) -> float | None:
    """HOUSE_TY 코드(예: "084.7402A")에서 전용면적(㎡)만 추출."""
    if not house_ty:
        return None
    m = _AREA_RE.match(house_ty.strip())
    return float(m.group(1)) if m else None


def parse_house_type(raw: dict) -> dict:
    """주택형별 상세(*Mdl) API 응답 1건을 파이프라인 공통 포맷으로 변환."""
    house_ty = raw.get("HOUSE_TY")
    return {
        "raw": raw,
        "house_ty": house_ty,
        "area_sqm": parse_area_from_house_ty(house_ty),
        # LTTOT_TOP_AMOUNT는 만원 단위 문자열(콤마 포함 가능) - main.py의 _to_int()에서 정수 변환
        "price_manwon": raw.get("LTTOT_TOP_AMOUNT"),
    }