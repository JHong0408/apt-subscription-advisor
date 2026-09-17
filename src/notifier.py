"""Slack Bot Token(Web API)으로 결과 전송.

기존에는 Incoming Webhook(SLACK_WEBHOOK_URL)으로 채널에 그냥 계속 쌓기만 했는데,
그러면 채널이 무한정 길어져서 오늘 알림을 찾기 힘들어진다.

그래서 "오늘 날짜" 단위로 헤더 메시지 1개를 올리고, 그날 발견된 공고들은
전부 그 헤더의 스레드 답글로 붙이는 방식으로 바꿨다. 채널 개수를 늘리지 않고도
(워크스페이스에 채널이 계속 새로 생기지 않음) 날짜별로 시각적으로 묶여서 보인다.

이 방식은 Incoming Webhook으로는 안 되고(스레드에 답글을 달려면 이전 메시지의
ts가 필요한데 Webhook 응답에는 그게 없음) Slack Web API(chat.postMessage)를
Bot Token으로 호출해야 한다. 필요한 설정:

1. https://api.slack.com/apps 에서 기존 Slack App(예: apt-advisor) 선택
2. "OAuth & Permissions" -> Bot Token Scopes에 `chat:write` 추가
   (초대 없이 공개 채널에 바로 쓰고 싶으면 `chat:write.public`도 추가)
3. 앱을 워크스페이스에 재설치(reinstall) -> "Bot User OAuth Token"(xoxb-...) 발급
4. 비공개 채널이면 그 채널에 앱을 초대(`/invite @앱이름`)
5. 채널 ID 확인: 채널 이름 클릭 -> 채널 세부 정보 맨 아래 (C로 시작하는 값)
6. 서버 환경변수에 SLACK_BOT_TOKEN(xoxb-...), SLACK_CHANNEL_ID(C...) 설정
   (기존 SLACK_WEBHOOK_URL은 더 이상 쓰지 않음)
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import requests

SLACK_API_URL = "https://slack.com/api/chat.postMessage"
THREAD_STATE_FILE = Path("daily_thread.json")


class SlackNotifierError(RuntimeError):
    pass


def _get_bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackNotifierError("환경변수 SLACK_BOT_TOKEN이 설정되지 않았습니다.")
    return token


def _get_channel_id() -> str:
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not channel:
        raise SlackNotifierError("환경변수 SLACK_CHANNEL_ID가 설정되지 않았습니다.")
    return channel


def _post(text: str, thread_ts: str | None = None) -> str:
    """chat.postMessage 호출. 성공하면 이 메시지의 ts(스레드 루트로 쓸 수 있는 값)를 반환."""
    payload = {"channel": _get_channel_id(), "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(
        SLACK_API_URL,
        headers={
            "Authorization": f"Bearer {_get_bot_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise SlackNotifierError(f"Slack API 오류: {data.get('error')}")
    return data["ts"]


def _load_thread_state() -> dict:
    if not THREAD_STATE_FILE.exists():
        return {}
    try:
        return json.loads(THREAD_STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_thread_state(state: dict) -> None:
    THREAD_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_or_create_daily_thread() -> str:
    """오늘 날짜의 스레드 루트 ts를 가져오거나, 없으면 헤더 메시지를 새로 올려서 만든다.

    daily_thread.json에 {"date": "...", "thread_ts": "..."}로 저장해두기 때문에,
    같은 날 여러 번(하루 여러 공고, 또는 파이프라인 재실행) 호출해도 헤더가
    중복으로 생기지 않고 같은 스레드에 계속 붙는다. 날짜가 바뀌면 새 헤더를 만든다.
    """
    today_str = date.today().isoformat()
    state = _load_thread_state()

    if state.get("date") == today_str and state.get("thread_ts"):
        return state["thread_ts"]

    header_text = f"📋 *{today_str} 청약 알림*"
    thread_ts = _post(header_text)

    _save_thread_state({"date": today_str, "thread_ts": thread_ts})
    return thread_ts


def send_slack_message(text: str) -> None:
    """공고 하나에 대한 메시지를 오늘 날짜 스레드의 답글로 전송한다.

    SLACK_BOT_TOKEN/SLACK_CHANNEL_ID가 설정되지 않았거나 호출이 실패하면
    콘솔에만 출력하고 넘어간다 (개인용 배치라 여기서 예외로 죽이지 않음).
    """
    try:
        thread_ts = get_or_create_daily_thread()
        _post(text, thread_ts=thread_ts)
        return
    except SlackNotifierError as e:
        print(f"[notifier] {e} - 콘솔에만 출력합니다.")
    except requests.RequestException as e:
        print(f"[notifier] Slack 요청 실패: {e} - 콘솔에만 출력합니다.")

    print(text)


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

    if references:
        lines.append("\n*🔎 참고 자료*")
        for ref in references:
            lines.append(f"> • <{ref['url']}|{ref['title']}> _({ref['source']})_")

    lines.append(f"\n*🤖 AI 추천*\n{recommendation}\n{'-' * 40}")
    return "\n".join(lines)