"""
TMAP(SK Open API) 대중교통 API 클라이언트.

회사 주소(위경도, profile.location에 미리 박아둠)에서 공고 주소까지 대중교통 추천
경로의 총 소요시간/거리를 계산한다. 직선거리가 아니라 실제 버스/지하철/도보를 조합한
경로 기준.

두 단계로 이뤄진다:
1. 지오코딩: 공고 주소(문자열) -> 좌표. 회사는 profile에 위경도를 미리 넣어두므로
   매번 지오코딩할 필요 없음(공고 주소만 매번 변환).
2. 대중교통 경로안내: 회사 좌표 -> 공고 좌표.

무료 한도(2026-09-29 기준 확인): 지오코딩 POI 검색 계열 일 수만 건, 경로안내(대중교통)
일 1,000건 - 이 프로젝트는 하루 10~20건 수준이라 여유 충분. ODsay와 달리 6개월 만료 같은
시한부 정책이 없는 상시 무료 한도라 GitHub Actions 자동화에 더 안전함.

※ 지오코딩 응답 스키마(coordinateInfo.coordinate[].lat/lon)는 2026-09-30 실제 실행 로그로
확인된 값이다. 경로안내(대중교통) 쪽 응답 스키마(metaData.plan.itineraries)는 아직 실제
응답으로 검증 전이라 잘못됐을 수 있다 - 그래서 파싱 실패도 예외로 던지지 않고 None을 반환해서
main.py가 다른 공고 처리를 계속할 수 있게 한다.
"""
from __future__ import annotations

import os
import re

import requests

GEOCODE_URL = "https://apis.openapi.sk.com/tmap/geo/fullAddrGeo"
TRANSIT_URL = "https://apis.openapi.sk.com/transit/routes"


class TmapAPIError(RuntimeError):
    pass


def _get_app_key() -> str:
    key = os.environ.get("TMAP_APP_KEY")
    if not key:
        raise TmapAPIError("환경변수 TMAP_APP_KEY가 설정되지 않았습니다.")
    return key


# "~공공주택지구 내 A-4블록"처럼 실제 지번/도로명이 아니라 사업지구 설명인 주소는
# fullAddrGeo가 못 찾을 수 있다(예: "회천지구 A10-1BL" 자체는 매칭 안 됨). 괄호 설명/
# 여러 동 나열/"번지"·"일원" 서술어를 걷어내고, 그래도 안 되면 시도+시군구+동 단위로
# 근사치를 시도한다(동 단위 자체는 정상적으로 매칭됨 - 확인 완료).
_ADDRESS_DESCRIPTIVE_WORDS = ["일원", "번지"]


def _strip_descriptive_words(address: str) -> str:
    """괄호 설명/여러 동 나열/"번지"·"일원" 서술어를 제거해서 순수 지번 주소 형태로 정리."""
    addr = re.sub(r"\([^)]*\)", " ", address)
    addr = addr.split(",")[0]
    for word in _ADDRESS_DESCRIPTIVE_WORDS:
        addr = addr.replace(word, " ")
    return re.sub(r"\s+", " ", addr).strip()


def _simplify_address(address: str) -> str:
    """위 정리로도 안 되면(번지 자체가 없는 "~공공주택지구 내 A-4블록" 같은 사업지구
    설명형 주소) 최후 수단으로 앞 3토큰(시도+시군구+동)만 남겨서 시도한다."""
    addr = _strip_descriptive_words(address)
    tokens = addr.split()
    return " ".join(tokens[:3]).strip()


def _request_geocode(address: str) -> tuple[float, float] | None:
    if not address:
        return None
    params = {
        "version": 1,
        "fullAddr": address,
        "coordType": "WGS84GEO",
        # F00: 지번(구주소)+도로명(새주소) 둘 다 허용. 청약홈 주소가 지번/도로명 섞여
        # 있는데, F02(도로명 전용)로 잘못 지정했던 게 이전까지 전부 400 났던 진짜 원인.
        "addressFlag": "F00",
        "page": 1,
        "count": 1,
        "format": "json",
    }
    resp = requests.get(
        GEOCODE_URL,
        params=params,
        headers={"appKey": _get_app_key(), "Accept": "application/json"},
        timeout=15,
    )
    # 임시 진단용: 응답 스키마를 실제로 본 적이 없어서 추정만으로 파싱하고 있다 - 원인
    # 파악되면 이 print는 지운다.
    print(f"[tmap_api][추적] geocode '{address}' -> status={resp.status_code}, body={resp.text[:500]!r}")
    if resp.status_code == 400:
        # TMAP 쪽 장애가 아니라 "이 문자열로는 주소를 못 찾음" - 회로차단기 대상 아님.
        return None
    resp.raise_for_status()
    data = resp.json()

    # 2026-09-30 실제 응답으로 확인된 진짜 스키마: coordinateInfo.coordinate[].lat/lon
    # (처음에 추정했던 newAddressList.newAddress/newLon/newLat는 틀린 스키마였음).
    candidates = data.get("coordinateInfo", {}).get("coordinate", [])
    if not candidates:
        return None
    top = candidates[0]
    return float(top["lon"]), float(top["lat"])


def geocode_address(address: str) -> tuple[float, float] | None:
    """도로명/지번 주소 문자열 -> (경도, 위도). 원문으로 못 찾으면 순서대로:
    1) "번지"/"일원" 서술어를 뗀 순수 지번 주소, 2) 시도+시군구+동 3토큰 근사치
    를 시도하고, 그래도 없으면 None (호출부에서 실패가 아니라 "정보 없음"으로 취급)."""
    result = _request_geocode(address)
    if result is not None:
        return result

    tried: set[str] = {address}
    for candidate in (_strip_descriptive_words(address), _simplify_address(address)):
        if not candidate or candidate in tried:
            continue
        tried.add(candidate)
        result = _request_geocode(candidate)
        if result is not None:
            print(f"[tmap_api] 주소 정리 후 지오코딩 성공: '{address}' -> '{candidate}'")
            return result
    return None


def find_transit_commute(start_lon: float, start_lat: float,
                          end_lon: float, end_lat: float) -> dict | None:
    """대중교통 추천 경로 1건의 총 소요시간(분)/총 거리(m)/환승 횟수. 경로 없으면 None."""
    body = {
        "startX": str(start_lon),
        "startY": str(start_lat),
        "endX": str(end_lon),
        "endY": str(end_lat),
        "count": 1,
        "lang": 0,
        "format": "json",
    }
    resp = requests.post(
        TRANSIT_URL,
        json=body,
        headers={
            "appKey": _get_app_key(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    # 임시 진단용: 위와 같은 이유.
    print(f"[tmap_api][추적] transit routes ({start_lon},{start_lat})->({end_lon},{end_lat}) -> status={resp.status_code}, body={resp.text[:500]!r}")
    resp.raise_for_status()
    data = resp.json()

    itineraries = data.get("metaData", {}).get("plan", {}).get("itineraries", [])
    if not itineraries:
        return None

    best = itineraries[0]
    return {
        "total_minutes": round(best["totalTime"] / 60),
        "total_distance_m": best.get("totalDistance"),
        "transfer_count": best.get("transferCount"),
    }


def find_commute_from_address(company_lon: float, company_lat: float,
                               destination_address: str) -> dict | None:
    """회사 좌표 -> 공고 주소 문자열까지의 대중교통 통근 정보. 주소를 못 찾거나(정상적인
    "정보 없음") 경로가 없으면 None을 반환하고, 이건 예외가 아니라서 main.py의 회로차단기
    카운트에도 안 잡힌다 - 진짜 서비스 장애(타임아웃/5xx 등)만 예외로 올라간다."""
    dest = geocode_address(destination_address)
    if dest is None:
        return None
    dest_lon, dest_lat = dest
    return find_transit_commute(company_lon, company_lat, dest_lon, dest_lat)
