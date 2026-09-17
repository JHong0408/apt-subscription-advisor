"""
공고 정보 + 시장분석 + 개인 프로필을 종합해 Claude에게 신청 여부 추천을 요청.
"""
from __future__ import annotations

import os
import json
import requests

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"


class ClaudeAdvisorError(RuntimeError):
    pass


def _get_api_key() -> str:
    key = os.environ.get("CLAUDE_API_KEY")
    if not key:
        raise ClaudeAdvisorError("환경변수 CLAUDE_API_KEY가 설정되지 않았습니다.")
    return key


def build_prompt_multi(analyzed_types: list[dict], profile: dict) -> str:
    """공고 하나(타입 여러 개 가능)를 한 번에 판단받기 위한 프롬프트.

    analyzed_types: [{"variant": notice_dict, "margin": {...}, "loan": {...}}, ...]
    (공고 자체는 다 동일하고, house_ty/area_sqm/price_manwon만 타입별로 다름)
    """
    base = analyzed_types[0]["variant"]

    type_blocks = []
    for a in analyzed_types:
        v, margin, loan = a["variant"], a["margin"], a["loan"]
        type_blocks.append(f"""
## 주택형 {v.get('house_ty') or '(미확인)'}
- 전용면적: {v.get('area_sqm')}㎡
- 분양가: {v.get('price_manwon')}만원
- 인근 평균 실거래가: {margin.get('avg_market_price')} (비교 {margin.get('comparable_count')}건)
- 분양가 대비 격차: {margin.get('margin_vs_avg')} ({margin.get('margin_pct_vs_avg')}%)
- 추정 대출 가능액: {loan.get('estimated_loan_capacity')}
- 추정 필요 현금: {loan.get('estimated_required_cash')}""")

    return f"""다음은 한 아파트 청약 공고(타입이 여러 개일 수 있음)와, 그것을 신청할지 고민 중인 사람의 정보다.
이 사람 입장에서 "신청할 만한지"를 판단해서 한국어로 짧고 구체적으로 답해줘.
타입이 여러 개면 그중 어떤 타입이 가장 나은지, 혹은 전부 비추천인지까지 명시해줘.
장점/단점을 나열하지 말고, 이 사람의 조건에 비춰 실제로 도움이 되는 결론과 이유를 5~8문장으로.

# 공고 정보
- 단지명: {base.get('house_name')}
- 위치: {base.get('address')}
- 공급구분: {base.get('supply_type')}
- 공고일: {base.get('recruit_date')}
{"".join(type_blocks)}

# 신청자 프로필
- 가용 현금: {profile.get('budget', {}).get('cash_available_krw')}
- 연소득: {profile.get('income', {}).get('annual_salary_krw')}
- 청약 가점: {profile.get('subscription_status', {}).get('subscription_score')}
- 유주택 여부: {profile.get('subscription_status', {}).get('is_homeowner')}
- 생애최초 지위 소진 여부: {profile.get('subscription_status', {}).get('used_first_time_buyer_benefit')}
- 회사 위치: {profile.get('location', {}).get('company_address')}
- 통근 허용 시간: {profile.get('location', {}).get('max_commute_minutes')}분
- 선호 조건: {profile.get('preferences', {})}
"""


def get_recommendation_multi(analyzed_types: list[dict], profile: dict) -> str:
    """공고 하나(타입 여러 개 가능)에 대해 Claude 호출 1번으로 통합 추천을 받는다."""
    prompt = build_prompt_multi(analyzed_types, profile)

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": _get_api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    text_parts = [
        block["text"] for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "\n".join(text_parts).strip()
    if text:
        return text

    # 텍스트가 비었을 때는 뭉개지 말고 실제 원인(stop_reason, 받은 블록 타입)을
    # 그대로 Slack 메시지에 노출시켜서 다음번엔 바로 원인을 알 수 있게 한다.
    stop_reason = data.get("stop_reason")
    block_types = [block.get("type") for block in data.get("content", [])]
    return (
        f"(추천 텍스트를 받지 못했습니다. stop_reason={stop_reason!r}, "
        f"content_block_types={block_types})"
    )