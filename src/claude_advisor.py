"""
공고 정보 + 시장분석 + 개인 프로필을 종합해 Claude에게 신청 여부 추천을 요청.
"""
from __future__ import annotations

import os
import json
import re
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


def _find_blog_reference(house_name: str, domain: str) -> dict | None:
    """Claude의 서버사이드 web_search 툴로 domain에서 이 단지 관련 글을 찾는다.

    우리 서버(GitHub Actions, Azure IP)에서 이 블로그들에 직접 요청하면 불안정하다:
    - mhb-blog.com: WAF가 403으로 막음 (2026-09-18 확인)
    - homedubu.com: sitemap/카테고리 페이지를 직접 스크래핑했는데, 에러 없이 빈
      결과가 오거나 sitemap이 XML이 아닌 걸 반환하는 등 원인 불명의 불안정한
      실패가 반복됨 (2026-09-21 확인)

    두 경우 다 Claude API의 web_search 툴로 옮기면 해결된다 - 검색이 Anthropic
    인프라에서 나가므로 이 문제들을 우회한다. allowed_domains로 검색 범위를
    domain 하나로 한정한다.

    못 찾거나 실패하면 None (참고자료는 "있으면 좋은" 보조 정보라 실패해도 조용히 넘어감).
    """
    prompt = f"""{domain} 사이트에서 "{house_name}" 아파트 청약과 관련된 글이 있는지 찾아줘.
관련 글이 여러 개(회차별로 따로 있는 경우 등)면 이 단지명과 가장 정확히 일치하는
글 하나만 골라. 있으면 그 글의 URL과 제목을, 없으면 둘 다 null로 해서 다른 설명
없이 아래 JSON 한 줄만 답해:

{{"url": "https://{domain}/..." 또는 null, "title": "글 제목" 또는 null}}"""

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": _get_api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 1024,
                "tools": [{
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "allowed_domains": [domain],
                    "max_uses": 3,
                }],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=55,  # 웹검색이 포함돼서 일반 텍스트 응답보다 오래 걸릴 수 있음
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[claude_advisor] {domain} 웹검색 실패({house_name!r}): {e}")
        return None

    text = "\n".join(
        block["text"] for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()

    m = re.search(r'\{.*"url".*\}', text, re.DOTALL)
    if not m:
        print(f"[claude_advisor] {domain} 웹검색: 응답에서 JSON을 못 찾음 ({house_name!r}): {text!r}")
        return None

    try:
        result = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"[claude_advisor] {domain} 웹검색: JSON 파싱 실패({house_name!r}): {e}")
        return None

    url = result.get("url")
    title = result.get("title")
    if not url:
        print(f"[claude_advisor] {domain} 웹검색: {house_name!r} 관련 글 없음")
        return None

    print(f"[claude_advisor] {domain} 웹검색: {house_name!r} -> {title!r} ({url})")
    return {"source": domain, "title": title or url, "url": url}


def find_mhb_blog_reference(house_name: str) -> dict | None:
    return _find_blog_reference(house_name, "mhb-blog.com")


def find_homedubu_reference(house_name: str) -> dict | None:
    return _find_blog_reference(house_name, "homedubu.com")


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