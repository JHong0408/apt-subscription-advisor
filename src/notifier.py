"""Slack Bot Token(Web API)으로 결과 전송.

이전엔 고정 채널 안에 "오늘 날짜" 헤더 메시지 1개 + 그날 공고들을 스레드 답글로
쌓는 방식이었는데, 가시성이 안 좋아서(스레드를 펼쳐봐야 내용이 보임) 매일 날짜별로
새 "채널"을 만들고 그 안에 일반 메시지로 쭉 쌓는 방식으로 바꿨다.

필요한 설정:

1. https://api.slack.com/apps 에서 기존 Slack App(예: apt-advisor) 선택
2. "OAuth & Permissions" -> Bot Token Scopes에 추가:
   - `chat:write` (메시지 전송, 이미 있었으면 그대로)
   - `channels:manage` (매일 새 공개 채널 생성)
   - `channels:read` (conversations.list - 채널 생성이 name_taken으로 실패했을 때
     기존 채널을 찾기 위한 복구용)
3. 앱을 워크스페이스에 재설치(reinstall) -> "Bot User OAuth Token"(xoxb-...) 재발급
   (스코프 추가 후에는 반드시 재설치해야 토큰에 새 권한이 반영됨)
4. 서버 환경변수에 SLACK_BOT_TOKEN(xoxb-...) 설정
   (SLACK_CHANNEL_ID는 더 이상 안 씀 - 채널을 매일 새로 만들기 때문)
5. (선택, 자동 초대용) 봇이 만든 새 채널에 자동으로 초대받고 싶으면
   SLACK_USER_ID(사용자 프로필의 "회원 ID", U로 시작)를 환경변수로 추가 설정.
   없으면 초대를 건너뛰고, 공개 채널이니 사이드바에서 직접 찾아 들어가면 됨.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SLACK_API_BASE = "https://slack.com/api"
CHANNEL_STATE_FILE = Path("daily_channel.json")

KST = timezone(timedelta(hours=9))


class SlackNotifierError(RuntimeError):
    pass


def _get_bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackNotifierError("환경변수 SLACK_BOT_TOKEN이 설정되지 않았습니다.")
    return token


def _today_kst_str() -> str:
    """GitHub Actions 러너는 UTC로 도니까, 채널 날짜는 반드시 KST 기준으로 계산한다."""
    return datetime.now(KST).date().isoformat()


def _slack_post(method: str, payload: dict) -> dict:
    resp = requests.post(
        f"{SLACK_API_BASE}/{method}",
        headers={
            "Authorization": f"Bearer {_get_bot_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _find_channel_id_by_name(name: str) -> str | None:
    """conversations.list를 순회하며 이름이 정확히 일치하는 공개 채널의 ID를 찾는다.

    conversations.create가 name_taken으로 실패했을 때(예: 이전 실행이 채널은
    만들어놓고 도중에 죽어서 daily_channel.json 저장을 못한 경우) 복구용으로 쓴다.
    """
    cursor = None
    while True:
        params = {"types": "public_channel", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{SLACK_API_BASE}/conversations.list",
            headers={"Authorization": f"Bearer {_get_bot_token()}"},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackNotifierError(f"Slack API 오류(conversations.list): {data.get('error')}")
        for ch in data.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return None


def _create_channel(name: str) -> str:
    """공개 채널을 새로 만들고 채널 ID를 반환한다. 이미 있으면(name_taken) 찾아서 재사용."""
    data = _slack_post("conversations.create", {"name": name, "is_private": False})
    if data.get("ok"):
        return data["channel"]["id"]

    if data.get("error") == "name_taken":
        existing = _find_channel_id_by_name(name)
        if existing:
            return existing

    raise SlackNotifierError(f"Slack API 오류(conversations.create): {data.get('error')}")


def _invite_user(channel_id: str) -> None:
    """SLACK_USER_ID가 설정돼 있으면 새 채널에 그 사용자를 자동 초대한다.

    설정 안 돼 있거나 실패해도(이미 참여 중 등) 조용히 넘어간다 - 초대는
    편의 기능일 뿐이라 실패해도 알림 자체는 계속 나가야 한다.
    """
    user_id = os.environ.get("SLACK_USER_ID")
    if not user_id:
        return

    data = _slack_post("conversations.invite", {"channel": channel_id, "users": user_id})
    if not data.get("ok") and data.get("error") != "already_in_channel":
        print(f"[notifier] 채널 자동 초대 실패({data.get('error')}) - 무시하고 계속 진행")


def _load_channel_state() -> dict:
    if not CHANNEL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(CHANNEL_STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_channel_state(state: dict) -> None:
    CHANNEL_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_or_create_daily_channel() -> str:
    """오늘(KST) 날짜의 채널 ID를 가져오거나, 없으면 새로 만든다.

    daily_channel.json에 {"date": "...", "channel_id": "..."}로 저장해두기 때문에,
    같은 날 여러 번(하루 여러 공고, 또는 파이프라인 재실행) 호출해도 채널이
    중복 생성되지 않고 같은 채널에 계속 쌓인다. 날짜가 바뀌면 새 채널을 만든다.
    """
    today_str = _today_kst_str()
    state = _load_channel_state()

    if state.get("date") == today_str and state.get("channel_id"):
        return state["channel_id"]

    channel_name = f"apt-{today_str}"
    channel_id = _create_channel(channel_name)
    _invite_user(channel_id)
    _post(channel_id, f"📋 *{today_str} 청약 알림*")

    _save_channel_state({"date": today_str, "channel_id": channel_id})
    return channel_id


def _post(channel_id: str, text: str) -> None:
    data = _slack_post("chat.postMessage", {"channel": channel_id, "text": text})
    if not data.get("ok"):
        raise SlackNotifierError(f"Slack API 오류(chat.postMessage): {data.get('error')}")


def send_slack_message(text: str) -> None:
    """공고 하나에 대한 메시지를 오늘(KST) 날짜 채널에 일반 메시지로 전송한다.

    SLACK_BOT_TOKEN이 없거나 호출이 실패하면 콘솔에만 출력하고 넘어간다
    (개인용 배치라 여기서 예외로 죽이지 않음).
    """
    try:
        channel_id = get_or_create_daily_channel()
        _post(channel_id, text)
        return
    except SlackNotifierError as e:
        print(f"[notifier] {e} - 콘솔에만 출력합니다.")
    except requests.RequestException as e:
        print(f"[notifier] Slack 요청 실패: {e} - 콘솔에만 출력합니다.")

    print(text)


def _format_date(date_str: str | None) -> str | None:
    """'20260918'과 '2026-09-18'를 둘 다 'YYYY-MM-DD'로 통일한다.

    cheongyak_api.parse_notice()의 주석대로, apt_remainder와 arbitrary_supply가
    접수일 형식을 다르게 준다(대시 있음/없음) - 화면에는 항상 같은 형식으로 보여준다.
    """
    if not date_str:
        return None
    digits = date_str.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return date_str


def _format_reception_period(start: str | None, end: str | None) -> str | None:
    start_fmt, end_fmt = _format_date(start), _format_date(end)
    if not start_fmt and not end_fmt:
        return None
    return f"{start_fmt or '?'} ~ {end_fmt or '?'}"


def format_notice_report_multi(
    analyzed_types: list[dict],
    recommendation: str,
    references: list[dict] | None = None,
) -> str:
    """공고 하나(타입 여러 개 가능)를 Slack 메시지 1개로 통합 포맷.

    analyzed_types: [{"variant": notice_dict, "margin": {...}, "loan": {...}}, ...]
    (공고 자체는 다 동일하고, house_ty/area_sqm/price_manwon만 타입별로 다름)
    references: reference_finder.find_all_references()가 찾아준
      [{"source": "mhb-blog.com", "title": "...", "url": "..."}, ...] 목록.
      없거나 못 찾았으면 None/빈 리스트 - 이 경우 섹션 자체를 생략한다.
    """
    base = analyzed_types[0]["variant"]
    notice_url = base.get("notice_url")
    period = _format_reception_period(
        base.get("reception_start_date"), base.get("reception_end_date")
    )

    lines = [
        f"*{base.get('house_name')}* ({base.get('address')})",
        f"> 공급구분: {base.get('supply_type') or '확인필요'}"
        + (f" · 청약 신청기간: {period}" if period else ""),
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

    if references:
        lines.append("\n*🔎 참고 자료*")
        for ref in references:
            lines.append(f"> • <{ref['url']}|{ref['title']}> _({ref['source']})_")

    lines.append(f"\n*🤖 AI 추천*\n{recommendation}\n{'-' * 40}")
    return "\n".join(lines)
