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


def build_prompt(notice: dict, margin: dict, loan: dict, profile: dict) -> str:
    return f"""다음은 한 아파트 청약 공고와, 그것을 신청할지 고민 중인 사람의 정보다.
이 사람 입장에서 "신청할 만한지"를 판단해서 한국어로 짧고 구체적으로 답해줘.
장점/단점을 나열하지 말고, 이 사람의 조건에 비춰 실제로 도움이 되는 결론과 이유를 3~5문장으로.

# 공고 정보
- 단지명: {notice.get('house_name')}
- 위치: {notice.get('address')}
- 공급유형: {notice.get('supply_type')}
- 전용면적: {notice.get('area_sqm')}㎡
- 분양가: {notice.get('price_manwon')}만원
- 공고일: {notice.get('recruit_date')}

# 시장 비교 (인근 실거래가 기준)
- 비교 가능 거래 건수: {margin.get('comparable_count')}
- 인근 평균 실거래가: {margin.get('avg_market_price')}
- 인근 최고 실거래가: {margin.get('max_market_price')}
- 분양가 대비 격차: {margin.get('margin_vs_avg')} ({margin.get('margin_pct_vs_avg')}%)

# 대략적 자금 계산 (근사치, 참고용)
- 추정 대출 가능액: {loan.get('estimated_loan_capacity')}
- 추정 필요 현금: {loan.get('estimated_required_cash')}
- 제한 요인: {loan.get('binding_constraint')}

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


def get_recommendation(notice: dict, margin: dict, loan: dict, profile: dict) -> str:
    prompt = build_prompt(notice, margin, loan, profile)

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": _get_api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 500,
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
    return "\n".join(text_parts).strip() or "(추천 텍스트를 받지 못했습니다.)"
