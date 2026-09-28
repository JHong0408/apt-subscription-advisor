"""
mhb-blog.com/homedubu.com 청약 분석 블로그의 참고자료를 Claude 웹검색으로 찾아온다.
AI 추천 문구는 만들지 않는다 - 참고 URL만 제공한다.
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
        detail = e.response.text[:300] if e.response is not None else ""
        print(f"[claude_advisor] {domain} 웹검색 실패({house_name!r}): {e} | 응답: {detail}")
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