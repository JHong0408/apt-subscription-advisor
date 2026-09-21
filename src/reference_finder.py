"""
homedubu.com / mhb-blog.com 두 청약 분석 블로그에서 찾은 참고 자료를 합쳐서
Slack 메시지의 "참고 자료" 링크로 붙이기 위한 모듈.

두 사이트 다 원래는 여기서 직접 스크래핑했었다(mhb-blog는 REST API 검색,
homedubu는 sitemap + 카테고리 페이지). 그런데:
- mhb-blog.com: WAF가 GitHub Actions(Azure) IP를 403으로 차단 (2026-09-18 확인)
- homedubu.com: sitemap/카테고리 페이지 응답이 원인 불명으로 불안정하게 실패
  (에러 없이 빈 결과, 또는 sitemap이 XML이 아닌 걸 반환) (2026-09-21 확인)

둘 다 claude_advisor.find_mhb_blog_reference() / find_homedubu_reference()가
Claude API의 web_search 툴(Anthropic 인프라에서 나가는 요청이라 위 문제들을
우회함)로 대신 찾아오도록 바꿨다. 이 파일은 이제 그 두 결과를 합치기만 한다.

⚠️ 둘 다 "찾으면 좋은" 보조 참고자료일 뿐이라, 못 찾아도 main.py 흐름은 그대로
   진행되게 조용히 빈 리스트를 반환한다.
"""
from __future__ import annotations


def find_all_references(
    mhb_reference: dict | None = None,
    homedubu_reference: dict | None = None,
) -> list[dict]:
    """두 사이트에서 찾은 참고 자료를 합쳐서 반환한다 (있는 것만, 순서: mhb-blog -> homedubu)."""
    return [ref for ref in (mhb_reference, homedubu_reference) if ref]
