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

※ 응답 JSON 스키마(특히 geocode의 coordinateInfo 경로, 경로안내의 metaData.plan.itineraries
경로)는 공식 문서 캡처가 불가능한 환경이라 외부 레퍼런스 기준으로 작성했다. 실제 실행 로그에서
스키마가 다르면 조정이 필요하다 - 그래서 파싱 실패도 예외로 던지지 않고 None을 반환해서
main.py가 다른 공고 처리를 계속할 수 있게 한다.
"""
from __future__ import annotations

import os

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


def geocode_address(address: str) -> tuple[float, float] | None:
    """도로명/지번 주소 문자열 -> (경도, 위도). 매칭되는 주소가 없으면 None."""
    params = {
        "version": 1,
        "fullAddr": address,
        "coordType": "WGS84GEO",
        "addressFlag": "F02",
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
    resp.raise_for_status()
    data = resp.json()

    candidates = (
        data.get("coordinateInfo", {}).get("newAddressList", {}).get("newAddress", [])
    )
    if not candidates:
        return None
    top = candidates[0]
    return float(top["newLon"]), float(top["newLat"])


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
    """회사 좌표 -> 공고 주소 문자열까지의 대중교통 통근 정보. 지오코딩/경로 중 하나라도
    실패하거나 결과가 없으면 None (main.py 쪽에서 실패로 취급해서 재시도 대상으로 둔다)."""
    dest = geocode_address(destination_address)
    if dest is None:
        return None
    dest_lon, dest_lat = dest
    return find_transit_commute(company_lon, company_lat, dest_lon, dest_lat)
