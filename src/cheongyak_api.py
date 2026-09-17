"""
한국부동산원 청약홈 분양정보 조회 서비스 클라이언트.

데이터 출처: 공공데이터포털 "한국부동산원_청약홈 분양정보 조회 서비스"
             (odcloud 기반, https://api.odcloud.kr)

⚠️ 중요: ENDPOINTS 중 apt_general 은 실제 사용 사례로 확인된 값이고,
         apt_remainder / arbitrary_supply 는 명명 규칙 추정치입니다.
         README.md의 "2) 청약홈 엔드포인트 확인" 절차대로 Swagger에서
         한 번 검증한 뒤 여기 값을 바로잡아 주세요.
"""
from __future__ import annotations

import os
import requests

BASE_URL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"

ENDPOINTS = {
    # 확인됨 (GitHub 공개 사례에서 실사용 확인: getAPTLttotPblancDetail)
    "apt_general": "/getAPTLttotPblancDetail",
    # TODO: Swagger에서 확인 필요 (추정: 무순위/잔여세대)
    "apt_remainder": "/getRemndrLttotPblancDetail",
    # TODO: Swagger에서 확인 필요 (추정: 임의공급) - 확실치 않으면 우선
    #       apt_general 결과에서 공급유형 필드로 필터링하는 방식으로 대체 가능
    "arbitrary_supply": "/getAsignSpclctSttusDetail",
}

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
    """API 원본 응답 1건을 파이프라인 공통 포맷으로 변환.

    ⚠️ 아래 필드명(HOUSE_NM, HSSPLY_ADRES 등)은 청약홈 API에서 흔히 쓰이는
    이름 규칙을 따른 추정치입니다. Swagger에서 실제 응답 예시를 한 번 보고
    다르면 이 함수만 고치면 됩니다 (다른 파일은 영향 없음).
    """
    return {
        "raw": raw,
        "house_name": raw.get("HOUSE_NM") or raw.get("houseName"),
        "address": raw.get("HSSPLY_ADRES") or raw.get("address"),
        "supply_type": raw.get("SPCLT_LWRESD_HOPE_AT") or raw.get("supplyType"),
        "recruit_date": raw.get("RCRIT_PBLANC_DE") or raw.get("recruitDate"),
        "area_sqm": raw.get("SUPLY_AR") or raw.get("area"),
        # ⚠️ 단위 미확인: 청약홈 분양가 필드는 통상 "만원" 단위로 내려오는 경우가
        # 많습니다(예: "95000" = 9억 5천만원). 이 값은 그대로 두고, 원 단위 변환은
        # main.py의 _to_int()에서 일괄 처리합니다. 실제 응답 받아보고 억/만원 여부가
        # 다르면 그쪽 변환 로직만 고치면 됩니다.
        "price_manwon": raw.get("LTTOT_TOP_AMOUNT") or raw.get("price"),
        "notice_id": raw.get("PBLANC_NO") or raw.get("noticeId"),
    }
