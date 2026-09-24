"""분석이 끝난 공고 1건을 apt-advisor(Cloudflare Worker + D1) 사이트로 전송.

이 사이트가 유일한 알림 채널이다 - Slack은 더 이상 쓰지 않는다. 로그인해서 전체 공고를
검색하는 것도, "신규" 배지를 보는 것도 전부 이 사이트에서 한다. site/src/index.js의
POST /api/sync를 호출한다.

동기화 실패(사이트 다운, 네트워크 오류 등)가 파이프라인 전체를 막으면 안 되므로 항상
예외를 삼키고 로깅만 한다 - main.py는 이 함수의 반환값(성공 여부)만 보고 계속 진행한다.
"""
from __future__ import annotations

import os

import requests

import cheongyak_api

SYNC_TIMEOUT_SECONDS = 10


def sync_notice(
    notice_id: str,
    analyzed_types: list[dict],
    recommendation: str,
    references: list[dict],
) -> bool:
    """사이트에 성공적으로 저장됐으면 True, 실패했거나 미설정이면 False."""
    site_url = os.environ.get("SITE_URL")
    sync_token = os.environ.get("SITE_SYNC_TOKEN")
    if not site_url or not sync_token:
        print("[site_sync] SITE_URL/SITE_SYNC_TOKEN 미설정, 사이트 동기화 건너뜀")
        return False

    base = analyzed_types[0]["variant"]
    payload = {
        "notice_id": notice_id,
        "house_name": base.get("house_name"),
        "address": base.get("address"),
        "region": cheongyak_api.extract_region(base.get("address", "") or ""),
        "supply_type": base.get("supply_type"),
        "reception_start_date": base.get("reception_start_date"),
        "reception_end_date": base.get("reception_end_date"),
        "notice_url": base.get("notice_url"),
        "recommendation": recommendation,
        "references": references,
        "types": [
            {
                "house_ty": a["variant"].get("house_ty"),
                "area_sqm": a["variant"].get("area_sqm"),
                "price_manwon": a["variant"].get("price_manwon"),
                "variant_id": a["variant"].get("variant_id"),
                "margin": a["margin"],
                "loan": a["loan"],
            }
            for a in analyzed_types
        ],
    }

    try:
        res = requests.post(
            f"{site_url.rstrip('/')}/api/sync",
            json=payload,
            headers={"Authorization": f"Bearer {sync_token}"},
            timeout=SYNC_TIMEOUT_SECONDS,
        )
        if not res.ok:
            print(f"[site_sync] 동기화 실패({notice_id}): {res.status_code} {res.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 - 동기화 실패로 알림 흐름을 막으면 안 됨
        print(f"[site_sync] 동기화 실패({notice_id}): {e}")
        return False
