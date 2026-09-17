"""
국토교통부 "아파트 매매 실거래가 상세 자료" API 클라이언트.

데이터 출처: 공공데이터포털 (data.go.kr/data/15126468)
요청주소: http://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev
※ 이 엔드포인트/파라미터명은 data.go.kr 공식 문서에서 확인된 값입니다.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import date

import requests

BASE_URL = "http://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"

# 서울 25개 자치구 법정동코드 앞 5자리 (행정표준코드관리시스템 기준, 안정적인 고정값)
SEOUL_LAWD_CD = {
    "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200",
    "광진구": "11215", "동대문구": "11230", "중랑구": "11260", "성북구": "11290",
    "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
    "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500",
    "구로구": "11530", "금천구": "11545", "영등포구": "11560", "동작구": "11590",
    "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710",
    "강동구": "11740",
}

# 경기도 31개 시/군 법정동코드 앞 5자리. 구가 있는 6개 시(수원/성남/안양/안산/고양/용인)는
# 시 전체 코드와 구별 코드를 모두 등록해 두어, 주소에서 구 이름까지 잡히면 더 좁은 범위로,
# 시 이름까지만 잡히면 시 전체로 실거래가를 조회할 수 있게 함.
# (data.go.kr 공식 문서 + 실거래가 API 활용 예제 기준으로 교차 확인한 값)
GYEONGGI_LAWD_CD = {
    "수원시": "41110",
    "수원시 장안구": "41111", "수원시 권선구": "41113",
    "수원시 팔달구": "41115", "수원시 영통구": "41117",
    "성남시": "41130",
    "성남시 수정구": "41131", "성남시 중원구": "41133", "성남시 분당구": "41135",
    "의정부시": "41150",
    "안양시": "41170",
    "안양시 만안구": "41171", "안양시 동안구": "41173",
    "부천시": "41190",
    "광명시": "41210",
    "평택시": "41220",
    "동두천시": "41250",
    "안산시": "41270",
    "안산시 상록구": "41271", "안산시 단원구": "41273",
    "고양시": "41280",
    "고양시 덕양구": "41281", "고양시 일산동구": "41285", "고양시 일산서구": "41287",
    "과천시": "41290",
    "구리시": "41310",
    "남양주시": "41360",
    "오산시": "41370",
    "시흥시": "41390",
    "군포시": "41410",
    "의왕시": "41430",
    "하남시": "41450",
    "용인시": "41460",
    "용인시 처인구": "41461", "용인시 기흥구": "41463", "용인시 수지구": "41465",
    "파주시": "41480",
    "이천시": "41500",
    "안성시": "41550",
    "김포시": "41570",
    "화성시": "41590",
    "광주시": "41610",
    "양주시": "41630",
    "포천시": "41650",
    "여주시": "41670",
    "연천군": "41800",
    "가평군": "41820",
    "양평군": "41830",
}

# 서울 + 경기 통합 조회용. main.py/cheongyak_api.py에서 뽑아낸 "지역 키"
# (예: "강북구", "수원시 영통구", "부천시")로 바로 조회할 수 있게 합쳐 둠.
ALL_LAWD_CD = {**SEOUL_LAWD_CD, **GYEONGGI_LAWD_CD}


class RtmsAPIError(RuntimeError):
    pass


def _get_service_key() -> str:
    key = os.environ.get("RTMS_SERVICE_KEY")
    if not key:
        raise RtmsAPIError("환경변수 RTMS_SERVICE_KEY가 설정되지 않았습니다.")
    return key


def fetch_trades(region_key: str, deal_ymd: str, num_of_rows: int = 500) -> list[dict]:
    """지역 키 + 계약년월(YYYYMM)로 실거래가 목록을 가져온다.

    region_key: "강북구", "수원시 영통구", "부천시" 처럼 ALL_LAWD_CD에 있는 키
    deal_ymd: "202609" 형식
    """
    lawd_cd = ALL_LAWD_CD.get(region_key)
    if not lawd_cd:
        raise RtmsAPIError(f"알 수 없는 지역명: {region_key}")

    params = {
        "serviceKey": _get_service_key(),
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "numOfRows": num_of_rows,
        "pageNo": 1,
    }

    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()

    # 이 API는 XML로 응답한다
    root = ET.fromstring(resp.text)
    items = root.findall(".//item")

    trades = []
    for item in items:
        def txt(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else None

        trades.append({
            "apt_name": txt("aptNm"),
            "area_sqm": float(txt("excluUseAr")) if txt("excluUseAr") else None,
            "deal_amount_10k_krw": txt("dealAmount"),  # 만원 단위 문자열, 콤마 포함될 수 있음
            "deal_year": txt("dealYear"),
            "deal_month": txt("dealMonth"),
            "deal_day": txt("dealDay"),
            "floor": txt("floor"),
            "build_year": txt("buildYear"),
            "dong": txt("umdNm"),
        })
    return trades


def recent_months(n: int = 3) -> list[str]:
    """오늘 기준 최근 n개월의 YYYYMM 리스트 (실거래가 신고 지연 감안해 이번달 포함)."""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return months


def find_comparable_trades(region_key: str, target_area_sqm: float,
                            tolerance_sqm: float = 5.0, months_back: int = 3) -> list[dict]:
    """같은 지역(구/시/군), 비슷한 면적대(±tolerance_sqm)의 최근 실거래 목록."""
    all_trades: list[dict] = []
    for ymd in recent_months(months_back):
        try:
            all_trades.extend(fetch_trades(region_key, ymd))
        except RtmsAPIError:
            continue

    if target_area_sqm is None:
        return all_trades

    return [
        t for t in all_trades
        if t["area_sqm"] is not None
        and abs(t["area_sqm"] - target_area_sqm) <= tolerance_sqm
    ]
