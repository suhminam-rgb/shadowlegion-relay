"""ShadowLegion 번역 릴레이.

내 계정 세션(TG_SESSION)으로 원본 방(TG_SOURCE_CHAT_ID)의 새 메시지를 읽고,
구글 번역(무료)으로 한국어로 옮겨 봇으로 번역 방(TG_DEST_CHAT_ID)에 보낸다.
마지막으로 처리한 메시지 id는 relay_state.json에 저장한다 (actions/cache로 유지).

로그 원칙: 공개 저장소라 Actions 로그를 누구나 볼 수 있으므로, 토큰·세션·채팅 ID·메시지
원문/번역문을 절대 출력하지 않는다. 예외는 종류 이름만 남긴다 (맨 아래 __main__ 참고).
"""
import asyncio
import json
import logging
import os
from datetime import timezone, timedelta

import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.utils import get_display_name

STATE_FILE = "relay_state.json"
MAX_PER_RUN = 50
MAX_FAILS = 3         # 같은 메시지 번역이 이만큼 연속(실행 기준) 실패하면 원문으로 보내고 넘어감
KST = timezone(timedelta(hours=9))

# Telethon 내부 로그와 asyncio의 "Task exception was never retrieved" traceback이
# 엔티티/메시지 정보를 찍지 않도록 끔
for _name in ("telethon", "asyncio"):
    logging.getLogger(_name).setLevel(logging.CRITICAL + 1)


def load_state():
    """{"last_id": 마지막으로 보낸 id, "fail_id": 번역 실패 중인 id, "fail_count": 연속 실패 횟수}"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state.get("last_id"), int):
            return state
    except (OSError, ValueError, AttributeError):
        pass
    return {"last_id": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def translate_chunk(text):
    # translate.google.com 스크래핑(deep-translator)은 GitHub 러너 IP에서 429가 나서
    # 무료 gtx 엔드포인트를 직접 호출. 예외/응답 내용은 원문이 섞일 수 있어 출력하지 않음
    for attempt in range(3):
        try:
            r = requests.post(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t"},
                data={"q": text}, timeout=30,
            )
            if r.ok:
                return "".join(seg[0] for seg in r.json()[0] if seg[0])
            print(f"번역 실패: HTTP {r.status_code}")
        except (requests.RequestException, ValueError, IndexError, TypeError) as e:
            print(f"번역 실패: {type(e).__name__}")
        time.sleep(5 * (attempt + 1))
    return None


def translate(text):
    """번역 결과, 실패하면 None (호출 쪽에서 다음 실행 때 다시 시도)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > 4500:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    chunks.append(cur)
    out = []
    for c in chunks:
        t = translate_chunk(c)
        if t is None:
            return None
        out.append(t)
    return "\n".join(out)


def send(bot_token, dest_chat_id, text):
    for i in range(0, len(text), 4000):
        # requests 예외 메시지와 raise_for_status()에는 토큰이 든 URL이 들어감 → 종류/상태코드만 출력
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data={"chat_id": dest_chat_id, "text": text[i:i + 4000],
                      "disable_web_page_preview": "true"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise SystemExit(f"텔레그램 전송 실패: {type(e).__name__}") from None
        if not r.ok:
            raise SystemExit(f"텔레그램 전송 실패: HTTP {r.status_code}")


async def main():
    # 환경변수 읽기도 main 안에서 (int() 실패 메시지에 값이 찍히는 것을 __main__ 가드로 막기 위해)
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    dest_chat_id = os.environ["TG_DEST_CHAT_ID"]
    source_chat_id = int(os.environ["TG_SOURCE_CHAT_ID"])
    # 수동 테스트용: REPLAY=N이면 최근 N개를 다시 보내고 상태는 건드리지 않음
    replay = int(os.environ.get("REPLAY") or 0)
    state = {"last_id": None} if replay else load_state()
    last_id = state["last_id"]
    async with TelegramClient(StringSession(os.environ["TG_SESSION"]),
                              int(os.environ["TG_API_ID"]),
                              os.environ["TG_API_HASH"]) as client:
        await client.get_dialogs()  # 새 세션은 엔티티 캐시가 비어 있어서 먼저 채움
        source = await client.get_entity(source_chat_id)

        if replay:
            msgs = await client.get_messages(source, limit=replay)
        elif last_id is None:
            # 첫 실행(상태 없음): 보내지 않고 현재 마지막 메시지 위치만 기록 → 이후 새 메시지부터 전송
            latest = await client.get_messages(source, limit=1)
            save_state({"last_id": latest[0].id if latest else 0})
            print("첫 실행: 위치만 기록")
            return
        else:
            msgs = await client.get_messages(source, min_id=last_id, limit=MAX_PER_RUN)
        msgs = sorted(msgs, key=lambda m: m.id)
        if not msgs:
            print("새 메시지 없음")
            return

        done = 0
        for m in msgs:
            text = (m.message or "").strip()
            if text:
                translated = translate(text)
                if translated is None:
                    fails = state.get("fail_count", 0) + 1 if state.get("fail_id") == m.id else 1
                    if fails < MAX_FAILS and not replay:
                        # 보내지도, last_id를 올리지도 않고 멈춤 → 다음 실행에서 이 메시지부터 재시도
                        state.update(fail_id=m.id, fail_count=fails)
                        save_state(state)
                        print(f"번역 실패 {fails}/{MAX_FAILS}회, 다음 실행에서 재시도")
                        break
                    # 연속 MAX_FAILS회 실패하면 영구히 막히지 않도록 원문으로 보내고 넘어감
                    translated = f"(번역 실패) {text}"
                    print(f"번역 {MAX_FAILS}회 연속 실패, 원문으로 전송")
                name = "" if m.post else get_display_name(await m.get_sender())
                when = m.date.astimezone(KST).strftime("%m/%d %H:%M")
                header = f"👤 {name} · {when}" if name else f"🕒 {when}"
                send(bot_token, dest_chat_id, f"{header}\n{translated}")
            done += 1
            if not replay:
                state = {"last_id": m.id}
                save_state(state)
        print(f"{done}개 처리" + ("" if replay else f", last_id={state['last_id']}"))


if __name__ == "__main__":
    # 처리되지 않은 예외의 traceback/메시지(채팅 ID, 원문 등)가 로그에 남지 않도록 종류만 출력
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except BaseException as e:
        raise SystemExit(f"실패: {type(e).__name__}") from None
