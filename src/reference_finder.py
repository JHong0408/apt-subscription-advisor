"""
homedubu.com / mhb-blog.com 두 청약 분석 블로그에서, 알림 보내는 단지와 관련된
게시글이 있으면 찾아서 Slack 메시지의 "참고 자료" 링크로 붙이기 위한 모듈.

- homedubu.com: robots.txt가 검색(`/?s=*`), REST API(`/wp-json/`),
  카테고리 페이지네이션(`/*/page/*`)을 전부 막아놔서, robots.txt가 허용하는
  sitemap(`wp-sitemap.xml`)으로 최근 게시글 URL 목록만 가져온 뒤, 그 게시글
  페이지의 <title>만 개별로 읽어서 단지명이 들어있는지 대조한다. 매 공고마다
  이 크롤링을 반복하면 비효율적이라, main.py에서 파이프라인 실행당 한 번만
  인덱스를 만들어서 재사용한다.

- mhb-blog.com: 원래는 여기도 워드프레스 REST API(`/wp-json/wp/v2/posts?search=`)로
  직접 검색했는데, GitHub Actions(Azure) IP를 WAF가 403으로 차단해서(실측 확인,
  2026-09-18) 이 파일에서는 더 이상 mhb-blog를 직접 호출하지 않는다. 대신
  claude_advisor.find_mhb_blog_reference()가 Claude API의 web_search 툴(Anthropic
  인프라에서 나가는 요청이라 차단을 우회함)로 찾아온 결과를 find_all_references()가
  인자로 받아서 homedubu 결과와 합친다.

⚠️ 둘 다 "찾으면 좋은" 보조 참고자료일 뿐이라, 실패하거나 못 찾아도
   (네트워크 오류, 게시글이 실제로 없음) main.py 흐름은 그대로 진행되게
   전부 조용히 None/빈 리스트를 반환한다.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

import requests

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HOMEDUBU_SITEMAP_INDEX = "https://homedubu.com/wp-sitemap.xml"


def _strip_html(text: str) -> str:
    """워드프레스 API가 돌려주는 title.rendered(HTML 엔티티 포함)를 순수 텍스트로."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _normalize_name(name: str) -> str:
    """단지명 비교용(넓게 찾기) 정규화: 괄호(차수 표기)/공백/구분자를 지운다.

    예: "더 리치먼드 미아(3차)" -> "더리치먼드미아"

    ⚠️ 회차 정보를 지워버리므로, 검색/후보 수집에만 쓰고 최종 선택에는 쓰지
    않는다 - "2차" 공고에 "1차" 글이 걸려도 여기서는 구분이 안 된다. 여러
    후보 중 실제로 회차까지 맞는 걸 고르는 건 _pick_best()가 원본(정규화 전)
    제목으로 따로 한다.
    """
    if not name:
        return ""
    name = re.sub(r"\([^)]*\)", "", name)   # 괄호 안(차수 등) 제거
    name = re.sub(r"\d+\s*차", "", name)      # "3차", "12차" 표기 제거
    name = re.sub(r"[\s\-_·]+", "", name)     # 공백/구분자 제거
    return name.strip()


_ROUND_RE = re.compile(r"\d+\s*차")


def _extract_round(name: str) -> str | None:
    """단지명 원본에서 회차 표기("2차", "12차" 등)만 뽑는다. 없으면 None."""
    m = _ROUND_RE.search(name or "")
    return re.sub(r"\s+", "", m.group(0)) if m else None


def _pick_best(candidates: list[dict], house_name: str) -> dict:
    """넓게 찾은 후보들 중, 회차까지 일치하는 걸 우선 선택한다.

    candidates는 이미 _normalize_name 기준으로 "관련 있다"고 판단된 것들이고,
    여기서는 원본(정규화 전) 제목에 공고의 회차 표기가 그대로 들어있는지만 본다.
    회차 표기가 없거나, 일치하는 후보가 하나도 없으면 기존 방식대로 첫 번째
    후보(API 관련도순/최신순)를 그대로 쓴다.
    """
    round_marker = _extract_round(house_name)
    if round_marker:
        for c in candidates:
            if round_marker in c["title"]:
                return c
    return candidates[0]


_POST_SITEMAP_RE = re.compile(r"sitemap-posts-post-\d+\.xml$")


def _get_homedubu_post_sitemap_urls() -> list[str]:
    """sitemap 인덱스에서 "게시글(post)" 타입 서브 sitemap URL만 뽑는다.

    워드프레스 기본 sitemap은 `wp-sitemap-posts-post-1.xml`(글), `wp-sitemap-posts-page-1.xml`
    (고정 페이지), `wp-sitemap-taxonomies-category-1.xml`(카테고리) 등으로 이름이 나뉘는데,
    "post"라는 단어만으로 필터링하면 "posts-page"에도 "post"가 부분 문자열로 들어있어서
    같이 걸려버린다. 그래서 "posts-post-숫자.xml" 패턴으로 정확히 맞춘다.
    """
    resp = requests.get(HOMEDUBU_SITEMAP_INDEX, timeout=10)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return [
        loc.text for loc in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS)
        if loc.text and _POST_SITEMAP_RE.search(loc.text)
    ]


def build_homedubu_index(max_posts: int = 40) -> list[dict]:
    """최근 게시글 max_posts개의 {title, url}을 모아온다.

    파이프라인 실행당 딱 한 번만 호출하도록(main.py) 설계됨 - 공고마다 매번
    다시 크롤링하면 요청 수가 너무 많아진다.
    """
    try:
        sitemap_urls = _get_homedubu_post_sitemap_urls()
    except (requests.RequestException, ET.ParseError) as e:
        print(f"[reference_finder] homedubu sitemap 인덱스 요청 실패: {e}")
        return []

    print(f"[reference_finder] homedubu: 서브 sitemap {len(sitemap_urls)}개 발견")

    entries: list[tuple[str, str]] = []
    for sitemap_url in sitemap_urls:
        try:
            resp = requests.get(sitemap_url, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"[reference_finder] homedubu 서브 sitemap 실패({sitemap_url}): {e}")
            continue
        for url_el in root.findall(".//sm:url", _SITEMAP_NS):
            loc = url_el.find("sm:loc", _SITEMAP_NS)
            lastmod = url_el.find("sm:lastmod", _SITEMAP_NS)
            if loc is not None and loc.text:
                entries.append((lastmod.text if lastmod is not None else "", loc.text))

    entries.sort(reverse=True)  # lastmod 최신순
    print(
        f"[reference_finder] homedubu: sitemap에 URL 총 {len(entries)}개 - "
        f"lastmod 최신 {min(max_posts, len(entries))}개만 <title> 조회 예정"
    )

    index: list[dict] = []
    fetch_fail = 0
    for _, url in entries[:max_posts]:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            fetch_fail += 1
            print(f"[reference_finder] homedubu 게시글 제목 조회 실패({url}): {e}")
            continue
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        if m:
            index.append({"title": _strip_html(m.group(1)), "url": url})

    print(f"[reference_finder] homedubu: 인덱스 {len(index)}건 구축 완료 (제목 조회 실패 {fetch_fail}건)")
    return index


def find_homedubu_reference(house_name: str, index: list[dict]) -> dict | None:
    """미리 만들어둔 homedubu 인덱스에서 단지명과 겹치는 게시글을 찾는다.

    인덱스 전체에서 관련 있어 보이는 후보를 다 모은 뒤, _pick_best()로 회차까지
    맞는 걸 우선 선택한다 (없으면 인덱스 순서상 첫 번째 = 최신 게시글).
    """
    query = _normalize_name(house_name)
    if not query or len(query) < 2:
        print(f"[reference_finder] homedubu: 검색어가 너무 짧아 스킵 (house_name={house_name!r})")
        return None

    candidates = [
        {"source": "homedubu.com", "title": entry["title"], "url": entry["url"]}
        for entry in index
        if query in _normalize_name(entry["title"]) or _normalize_name(entry["title"]) in query
    ]

    if not candidates:
        print(f"[reference_finder] homedubu: 인덱스 {len(index)}건 중 query={query!r} 매칭 0건")
        return None

    best = _pick_best(candidates, house_name)
    print(f"[reference_finder] homedubu: query={query!r} 매칭 {len(candidates)}건 중 선택 -> {best['title']!r}")
    return best


def find_all_references(
    house_name: str,
    homedubu_index: list[dict],
    mhb_reference: dict | None = None,
) -> list[dict]:
    """두 사이트에서 찾은 참고 자료를 합쳐서 반환한다 (있는 것만, 순서: mhb-blog -> homedubu).

    mhb_reference: claude_advisor.find_mhb_blog_reference()가 미리 찾아온 결과를
    그대로 받는다. mhb-blog.com은 우리 서버 IP를 막아서(403) 이 파일에서 직접
    검색할 수 없기 때문 - main.py가 호출 순서를 책임진다.
    """
    print(f"[reference_finder] --- 참고자료 검색 시작: house_name={house_name!r} ---")
    refs = []

    if mhb_reference:
        refs.append(mhb_reference)

    homedubu = find_homedubu_reference(house_name, homedubu_index)
    if homedubu:
        refs.append(homedubu)

    print(f"[reference_finder] --- 결과: mhb={'있음' if mhb_reference else '없음'}, homedubu={'있음' if homedubu else '없음'} ---")
    return refs