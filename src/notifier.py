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


def format_notice_report(notice: dict, margin: dict, loan: dict, recommendation: str) -> str:
    price_manwon = notice.get("price_manwon")
    house_ty = notice.get("house_ty")
    notice_url = notice.get("notice_url")
    lines = [
        f"*{notice.get('house_name')}* ({notice.get('address')})",
        f"> 공급구분: {notice.get('supply_type') or '확인필요'}"
        f" · 주택형: {house_ty or '(주택형 상세 미확인)'}"
        f" · 면적: {notice.get('area_sqm')}㎡"
        f" · 분양가: {price_manwon}만원",
        f"> 인근 평균 실거래가: {margin.get('avg_market_price')} "
        f"(격차 {margin.get('margin_pct_vs_avg')}%)",
        f"> 추정 필요 현금: {loan.get('estimated_required_cash')}",
    ]
    if notice_url:
        lines.append(f"> 공고 원문: {notice_url}")
    lines.append(f"\n*🤖 AI 추천*\n{recommendation}\n{'-' * 40}")
    return "\n".join(lines)