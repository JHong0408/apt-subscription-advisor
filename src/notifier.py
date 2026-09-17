"""Slack Incoming Webhook으로 결과 전송."""
from __future__ import annotations

import os
import requests


def send_slack_message(text: str) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[notifier] SLACK_WEBHOOK_URL 미설정 - 콘솔에만 출력합니다.")
        print(text)
        return

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()


def format_notice_report_multi(analyzed_types: list[dict], recommendation: str) -> str:
    """공고 하나(타입 여러 개 가능)를 Slack 메시지 1개로 통합 포맷.

    analyzed_types: [{"variant": notice_dict, "margin": {...}, "loan": {...}}, ...]
    (공고 자체는 다 동일하고, house_ty/area_sqm/price_manwon만 타입별로 다름)
    """
    base = analyzed_types[0]["variant"]
    notice_url = base.get("notice_url")

    lines = [
        f"*{base.get('house_name')}* ({base.get('address')})",
        f"> 공급구분: {base.get('supply_type') or '확인필요'}",
    ]

    for a in analyzed_types:
        v, margin, loan = a["variant"], a["margin"], a["loan"]
        lines.append(
            f"> • {v.get('house_ty') or '(주택형 미확인)'} "
            f"({v.get('area_sqm')}㎡ / {v.get('price_manwon')}만원) "
            f"- 시세대비 {margin.get('margin_pct_vs_avg')}% · "
            f"필요현금 {loan.get('estimated_required_cash')}"
        )

    if notice_url:
        lines.append(f"> 공고 원문: {notice_url}")
    lines.append(f"\n*🤖 AI 추천*\n{recommendation}\n{'-' * 40}")
    return "\n".join(lines)