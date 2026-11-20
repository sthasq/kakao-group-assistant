#!/usr/bin/env python3
"""Run a periodic KakaoTalk topic reader and its local browser interface."""
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import decisions
from chat_memory import ChatMemory

ROOT = Path(__file__).resolve().parent
ROOM = os.environ.get("KAKAO_ROOM", "").strip()
ROOM_ID = hashlib.sha256(ROOM.encode()).hexdigest()[:16]
RUNTIME = ROOT / ".runtime" / ROOM_ID
CLI = Path(os.environ.get("OPENKAKAO_CLI", str(Path.home() / ".local/bin/openkakao-cli"))).expanduser()
HOST, PORT = "127.0.0.1", 8765
ORIGIN = f"http://{HOST}:{PORT}"
TZ = ZoneInfo("Asia/Seoul")
READ_LIMIT = 100
READ_ATTEMPTS = 3
READ_RETRY_DELAY = 3
RECOVERY_FILE = RUNTIME / "recover-hour"
LEGACY_PENDING_FILE = RUNTIME / "pending-hours.json"
PENDING_FILE = RUNTIME / "pending-windows.json"
SETTINGS_FILE = RUNTIME / "topic-settings.json"
HISTORY_FILE = RUNTIME / "digest-history.json"
CHAT_MEMORY_FILE = RUNTIME / "room-memory.sqlite3"
WINDOW_ERRORS = {"main_window_not_visible", "chat_window_not_open", "kakaotalk_not_running"}
CHECK_INTERVAL = 60
BACKLOG_HOURS = 12
DEFAULT_SETTINGS = dict(interval_minutes=10, context_minutes=120, poll_seconds=1, quiet_seconds=4, read_limit=READ_LIMIT, post_to_chat=False,
                        questions=[decisions.ASSISTANT_QUESTION], api_options={}, full_answers=False)
settings = DEFAULT_SETTINGS.copy()
STAMP = re.compile(r"^(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(?:(오전|오후)\s*(\d{1,2}):(\d{2}))?$")
MEDIA = re.compile(r"\[(사진|동영상|이모티콘|파일|음성 메시지|공유|알 수 없음)\]")
ASSETS = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/settings.js": ("settings.js", "text/javascript")}

lock = threading.Lock()
wake = threading.Event()
events = deque(maxlen=15)
pending_windows = []
digest_history = []
state = dict(enabled=False, phase="paused", next_run=None, last_check=None, last_send=None, memory_count=0,
             target_count=0, sent_count=0, latest=None, api_test="idle", pending_count=0)
revision = 0
next_start = None


def now():
    return datetime.now(TZ)


def window_boundary(at, period):
    anchor = datetime(2000, 1, 1, tzinfo=TZ)
    return anchor + (at - anchor) // period * period


def validated_settings(data):
    if not isinstance(data, dict):
        raise ValueError("설정 형식을 확인해 주세요.")
    data = dict(data)
    data.setdefault("context_minutes", 120)
    data.setdefault("poll_seconds", 1)
    data.setdefault("quiet_seconds", 4)
    for key, minimum, maximum in (("interval_minutes", 1, 60), ("context_minutes", 10, 360), ("read_limit", 10, 100), ("poll_seconds", 1, 30), ("quiet_seconds", 1, 30)):
        value = data.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            label = {"interval_minutes": "확인 간격", "context_minutes": "맥락 범위", "read_limit": "최대 메시지 수", "poll_seconds": "탐지 간격", "quiet_seconds": "대화 대기 시간"}[key]
            raise ValueError(f"{label}는 {minimum}–{maximum} 사이의 정수여야 해요.")
    for key in ("post_to_chat", "full_answers"):
        if type(data.get(key)) is not bool:
            raise ValueError("전송 옵션을 확인해 주세요.")
    decisions.validate_options(data.get("questions"), data.get("api_options"))
    return {key: data[key] for key in DEFAULT_SETTINGS}


def save_settings(updated):
    global next_start
    with lock:
        changed = settings["interval_minutes"] != updated["interval_minutes"]
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(json.dumps(updated, ensure_ascii=False))
        pending.replace(SETTINGS_FILE)
        settings.update(updated)
        if changed and state["enabled"]:
            period = timedelta(minutes=updated["interval_minutes"])
            boundary = window_boundary(now(), period)
            if next_start is None:
                next_start = boundary
            state["next_run"] = boundary + period
        record("control", f"판독 설정을 저장했어요. · {updated['interval_minutes']}분 간격 · 질문 {len(updated['questions'])}개")
        wake.set()


def classify(messages, options, previous=None):
    return decisions.classify(messages, options["questions"], options["api_options"], options["full_answers"], previous)


def remember_digest(end, answers):
    items = [item for answer in answers if answer["type"] == "digest" for item in answer["items"]]
    if not items:
        return
    cutoff = now() - timedelta(days=1)
    entries = [entry for entry in digest_history if datetime.fromisoformat(entry["end"]) >= cutoff]
    entries.append({"end": end.isoformat(), "items": items})
    entries = entries[-144:]
    temporary = HISTORY_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, ensure_ascii=False))
    temporary.replace(HISTORY_FILE)
    digest_history[:] = entries


def record(kind, detail):
    events.appendleft({"kind": kind, "detail": detail, "time": now().isoformat(timespec="seconds")})


def store_pending(windows):
    windows = sorted(set(windows))
    if windows == pending_windows and PENDING_FILE.exists():
        return
    temporary = PENDING_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps([[end.isoformat(), start.isoformat()] for end, start in windows]))
    temporary.replace(PENDING_FILE)
    pending_windows[:] = windows
    state["pending_count"] = len(windows)


def set_enabled(enabled):
    global revision, next_start
    with lock:
        if state["enabled"] == enabled:
            return
        path = RUNTIME / "enabled"
        pending = path.with_suffix(".tmp")
        pending.write_text("1" if enabled else "0")
        pending.replace(path)
        revision += 1
        period = timedelta(minutes=settings["interval_minutes"])
        next_start = window_boundary(now(), period) if enabled else None
        scheduled = next_start + period if enabled else None
        phase = "window_wait" if enabled and pending_windows else "watching" if enabled else "paused"
        state.update(enabled=enabled, phase=phase, next_run=scheduled)
        record("control", f"{settings['interval_minutes']}분 간격 주제 판독을 켰어요." if enabled else "주제 판독을 일시 정지했어요.")
        wake.set()


def cli_json(command, *arguments):
    completed = subprocess.run(
        [str(CLI), command, *arguments, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = None
    if completed.returncode or not isinstance(result, dict) or "error" in result:
        error = result.get("error") if isinstance(result, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        raise RuntimeError(code or "cli_unavailable")
    return result


def read_room(limit=READ_LIMIT):
    result = cli_json("ax-read", ROOM, "-n", str(limit), "--open-if-needed")
    if result.get("chat_name") != ROOM:
        raise RuntimeError("room_mismatch")
    return result["messages"]


def conversation(rows, start, end, fresh_start=None, live=False):
    """Inherit the preceding displayed time for messages grouped under the same date."""
    messages, speakers = [], {}
    time = None
    for row in rows:
        stamp = (row.get("time") or "").strip()
        match = STAMP.fullmatch(stamp)
        if match:
            year, month, day = map(int, match.group(1, 2, 3))
            period, hour, minute = match.group(4, 5, 6)
            if hour is not None:
                clock_hour = int(hour) % 12 + (12 if period == "오후" else 0)
                time = datetime(year, month, day, clock_hour, int(minute), tzinfo=TZ)
            elif time is not None and (time.year, time.month, time.day) != (year, month, day):
                time = None
        elif stamp:
            time = None

        text = (row.get("text") or "").strip()
        if (
            time is None or not start <= time < end or not text
            or text == "여기까지 읽었습니다." or MEDIA.fullmatch(text)
            or text.startswith((decisions.NOTICE_PREFIX, "[단톡방 주제 판독]", "[자동 판정]"))
            or (row.get("via_openkakao") and not (live and text.startswith(decisions.ASSISTANT_PREFIX)))
        ):
            continue
        sender = row.get("sender")
        alias = "나" if row.get("outgoing") else speakers.setdefault(sender, f"참여자 {len(speakers) + 1}")
        if live and row.get("outgoing") and text.startswith(decisions.ASSISTANT_PREFIX):
            alias = "AI 어시스턴트"
        elif live and not row.get("outgoing"):
            alias = "참여자 " + hashlib.sha256(str(sender).encode()).hexdigest()[:8]
        messages.append({"speaker": alias, "text": text[:1000], "time": time.isoformat(timespec="minutes"),
                         "new": fresh_start is None or time >= fresh_start})
        if live:
            identity = json.dumps([time.isoformat(), sender if not row.get("outgoing") else "self", text], ensure_ascii=False)
            messages[-1]["_key"] = hashlib.sha256(identity.encode()).hexdigest()
    return messages


def percent(probability):
    if 0 < probability < 0.001:
        return "<0.1%"
    return f"{probability * 100:.1f}".removesuffix(".0") + "%"


def render_answers(answers):
    names = {"topic": "주요 주제", "tone": "분위기"}
    sections = []
    for answer in answers:
        title = names.get(answer["name"], answer["name"])
        lines = [f"● {title}"]
        kind = answer["type"]
        if kind == "assistant":
            if answer["reply"]:
                sections.append(answer["reply"])
            continue
        if kind == "digest":
            if answer["items"]:
                sections.append("\n".join(f"• [{item['category']}] {item['text']}" for item in answer["items"]))
            continue
        if kind == "predicate":
            lines.append(f"조건 성립 추정 확률 {percent(answer['probability'])}")
        else:
            if kind == "score":
                score = f"{answer['score']:.2f}".rstrip("0").rstrip(".")
                lines.append(f"척도 점수 {score} · 확신도 {percent(answer['confidence'])}")
            elif not answer.get("probabilities"):
                lines.append(f"{answer['choice']} ({percent(answer['confidence'])})")
            ranked = sorted(answer.get("probabilities", []), key=lambda value: value["probability"], reverse=True)
            if kind != "ranked":
                ranked = ranked[:3]
            for position, entry in enumerate(ranked, 1):
                label = entry.get("label", entry["value"])
                lines.append(f"{position}. {label} ({percent(entry['probability'])})")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def render_notice(start, end, answers):
    body = render_answers(answers)
    return f"{decisions.NOTICE_PREFIX} {start:%H:%M}–{end:%H:%M}\n\n{body}" if body else ""


def run_pending(cycle):
    with lock:
        options = settings.copy()
        windows = list(pending_windows)
    for attempt in range(1, READ_ATTEMPTS + 1):
        with lock:
            if cycle != revision:
                return
        try:
            rows = read_room(options["read_limit"])
            ready = [(window, conversation(rows, window[1], window[0])) for window in windows]
            break
        except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            raw = str(error)
            code = raw if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", raw) else type(error).__name__
            with lock:
                if cycle != revision:
                    return
                if code in WINDOW_ERRORS:
                    if state["phase"] != "window_wait":
                        record("watch", f"카카오톡 창이 보이면 판독을 이어서 처리해요. · {code}")
                    state["phase"] = "window_wait"
                    return
                if attempt == READ_ATTEMPTS:
                    state["phase"] = "access_error" if code == "accessibility_not_granted" else "room_error"
                    record("error", f"카카오톡 읽기 {attempt}회 실패 · {code}")
                    return
                record("watch", f"카카오톡 읽기를 재시도해요. · {code}")
            time.sleep(READ_RETRY_DELAY)
    with lock:
        if cycle != revision:
            return
        empty = {window for window, messages in ready if not messages}
        nonempty = [(window, messages) for window, messages in ready if messages]
        if empty:
            record("watch", f"읽을 수 있는 범위에 판독할 텍스트가 없는 구간 {len(empty)}건을 정리했어요.")
        if not nonempty:
            store_pending(window for window in pending_windows if window not in empty)
            state["target_count"] = 0
            state["phase"] = "watching"
            return
        # Prefer the most recent due window, and claim it before external work to avoid duplicate sends.
        (end, start), messages = max(nonempty, key=lambda entry: entry[0])
        completed = empty | {(end, start)}
        store_pending(window for window in pending_windows if window not in completed)
        state.update(phase="generating", target_count=len(messages))
    try:
        context_start = min(start, end - timedelta(minutes=options["context_minutes"]))
        messages = conversation(rows, context_start, end, fresh_start=start)
        previous = [entry for entry in digest_history if datetime.fromisoformat(entry["end"]) >= end - timedelta(days=1)][-48:]
        answers = classify(messages, options, previous)
    except (OSError, ValueError, subprocess.SubprocessError):
        with lock:
            if cycle == revision:
                state["phase"] = "model_error"
                record("error", "모델에서 주제 판정을 받지 못했어요.")
        return

    with lock:
        if cycle != revision:
            return
        notice = render_notice(start, end, answers)
        state["latest"] = {"start": start.isoformat(), "end": end.isoformat(), "answers": answers, "notice": notice}
        if not notice:
            state["phase"] = "watching"
            record("watch", f"{start:%H:%M}–{end:%H:%M} · 새롭게 공유할 중요 내용이 없어요.")
            return
        if not settings["post_to_chat"]:
            state["phase"] = "watching"
            record("watch", f"{start:%H:%M}–{end:%H:%M} · 판독 결과를 브라우저에 표시했어요.")
            return
        try:
            cli_json("local-send", ROOM, notice,
                     "--unattended", "--allow-non-interactive-send", "--yes", "--no-prefix")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            state["phase"] = "send_error"
            record("error", "카톡 전송을 확인하지 못했어요. 자동으로 재전송하지 않아요.")
        else:
            state["sent_count"] += 1
            state.update(phase="watching", last_send=now().isoformat(timespec="seconds"))
            try:
                remember_digest(end, answers)
            except OSError:
                record("error", "이전 요약을 저장하지 못했어요.")
            record("sent", f"{start:%H:%M}–{end:%H:%M} · 요약을 전송했어요.")


def monitor():
    global next_start
    retry_at = None
    while True:
        if any(q["type"] == "assistant" for q in settings["questions"]):
            live_monitor()
            retry_at = None
            continue
        with lock:
            at = now()
            scheduled = state["next_run"]
            cycle = None
            wait = None
            if scheduled is not None:
                period = timedelta(minutes=settings["interval_minutes"])
                floor = window_boundary(at, period)
                cutoff = at - timedelta(hours=BACKLOG_HOURS)
                due = [window for window in pending_windows if cutoff <= window[0] <= at]
                if scheduled <= at:
                    first = max(scheduled, floor - timedelta(hours=BACKLOG_HOURS) // period * period)
                    start = next_start if first == scheduled and next_start is not None else first - period
                    for offset in range((floor - first) // period + 1):
                        end = first + offset * period
                        if cutoff <= end and start < end:
                            due.append((end, start))
                        start = end
                    next_start = floor
                    state["next_run"] = floor + period
                store_pending(due)
                if pending_windows and (retry_at is None or retry_at <= at):
                    cycle = revision
                    state["last_check"] = at.isoformat(timespec="seconds")
                else:
                    deadline = min(state["next_run"], retry_at) if pending_windows else state["next_run"]
                    wait = min(CHECK_INTERVAL, (deadline - at).total_seconds())
            if cycle is None:
                wake.clear()
        if cycle is None:
            wake.wait(wait)
            continue
        run_pending(cycle)
        with lock:
            retry_at = now() + timedelta(seconds=CHECK_INTERVAL) if pending_windows else None


CALL = re.compile(r"(?i)(?:@\s*(?:ai|gpt|챗지피티|어시스턴트)|(?:^|[\s,])(?:ai|gpt|챗지피티|어시스턴트)\s*(?:야|아|님|[:,：])|(?:^|[\s,])(?:봇아|봇이야))")
FOLLOWUP = re.compile(r"\?|[？]|(?:그럼|그러면|근데|왜|어떻게|뭐야|맞아|맞음|설명|더 알려|다시 알려|예시|가능해|아니야|아닌데|어떤|얼마|뭔데)")


def new_tail(previous, current):
    """Find appended messages without replaying a history snapshot on reconnect."""
    if previous == current:
        return 0
    if not previous:
        return len(current)
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return len(current) - size
    # The UI can prepend older rows when loading history.
    for index in range(len(current) - 1, -1, -1):
        if current[index] == previous[-1]:
            return len(current) - index - 1
    return 0


def live_monitor():
    try:
        memory = ChatMemory(CHAT_MEMORY_FILE)
    except (OSError, sqlite3.Error):
        with lock:
            state["phase"] = "room_error"
            record("error", "대화 메모리를 열지 못했어요.")
        wake.wait(10)
        return
    previous = None
    last_read_at = None
    waiting = set()
    first_at = last_at = last_auto = 0.0
    active_revision = None
    while True:
        with lock:
            if not any(q["type"] == "assistant" for q in settings["questions"]):
                memory.close()
                return
            options = settings.copy()
            cycle = revision
            enabled = state["enabled"]
            state["next_run"] = now() + timedelta(seconds=options["poll_seconds"]) if enabled else None
            if not enabled:
                previous, waiting = None, set()
                last_read_at = None
            if active_revision != cycle:
                previous, waiting, active_revision = None, set(), cycle
                last_read_at = None
        if not enabled:
            wake.wait(1)
            wake.clear()
            continue
        tick = time.monotonic()
        try:
            at = now()
            rows = read_room(options["read_limit"])
            messages = conversation(rows, at - timedelta(hours=24), at + timedelta(minutes=1), live=True)
        except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            with lock:
                if cycle == revision:
                    phase = "access_error" if str(error) == "accessibility_not_granted" else "window_wait" if str(error) in WINDOW_ERRORS else "room_error"
                    if state["phase"] != phase:
                        record("watch", "카톡 접근 대기 · " + (str(error) if str(error) in WINDOW_ERRORS else type(error).__name__))
                    state["phase"] = phase
            wake.wait(max(3, options["poll_seconds"]))
            wake.clear()
            continue
        keys = [m["_key"] for m in messages]
        # Empty accessibility snapshots can be transient; do not mistake them for a cleared room.
        baseline = previous is None or (previous and keys and not set(previous).intersection(keys))
        count = new_tail(previous, keys) if previous is not None and keys else 0
        try:
            added = memory.ingest(messages, None if baseline else count, at)
            if keys or previous is None:
                previous = keys
            new_ids = {ident for ident, speaker, stamp in added if speaker != "AI 어시스턴트" and
                       (not baseline or (last_read_at is not None and datetime.fromisoformat(stamp) > last_read_at.replace(second=0, microsecond=0)))}
            last_read_at = at
            if new_ids:
                first_at = first_at if waiting else tick
                waiting.update(new_ids)
                last_at = tick
            messages = memory.context(at, options["context_minutes"], options["read_limit"])
            memory_count = memory.count()
        except (OSError, sqlite3.Error):
            with lock:
                state["phase"] = "room_error"
                record("error", "대화 메모리 저장에 실패했어요.")
            wake.wait(3)
            continue
        waiting.intersection_update(m["id"] for m in messages)
        recent_ai = next((m for m in reversed(messages) if m["speaker"] == "AI 어시스턴트"), None)
        recent_ai_id = recent_ai["id"] if recent_ai else None
        recent_ai_time = datetime.fromisoformat(recent_ai["time"]) if recent_ai else None
        for message in messages:
            message["new"] = message["id"] in waiting and message["speaker"] != "AI 어시스턴트"
            message["explicit"] = message["new"] and bool(CALL.search(message["text"]))
            message["followup_candidate"] = bool(message["new"] and recent_ai_id is not None and
                message["id"] > recent_ai_id and
                at - recent_ai_time < timedelta(minutes=5) and
                FOLLOWUP.search(message["text"]))
            message.pop("id")
        explicit = any(m["explicit"] for m in messages)
        followup = any(m["followup_candidate"] for m in messages)
        due = bool(waiting) and (explicit or followup or ((tick - last_at >= options["quiet_seconds"] or tick - first_at >= 15) and tick - last_auto >= 45))
        with lock:
            if cycle != revision:
                continue
            state.update(phase="generating" if due else "watching", last_check=at.isoformat(timespec="seconds"), target_count=len(waiting), memory_count=memory_count)
        if due:
            waiting.clear()
            if not explicit and not followup:
                last_auto = tick
            try:
                answers = classify(messages, options)
            except (OSError, ValueError, subprocess.SubprocessError, KeyError):
                with lock:
                    if cycle == revision:
                        state["phase"] = "model_error"
                        record("error", "AI 답변 요청에 실패했어요.")
            else:
                reply = render_answers(answers)
                notice = f"{decisions.ASSISTANT_PREFIX} {reply}" if reply else ""
                with lock:
                    if cycle != revision:
                        continue
                    state["latest"] = {"answers": answers, "notice": notice, "end": at.isoformat()}
                    if not notice:
                        record("watch", "참여할 필요가 없어 답변을 생략했어요.")
                    elif settings["post_to_chat"]:
                        try:
                            cli_json("local-send", ROOM, notice, "--unattended", "--allow-non-interactive-send", "--yes", "--no-prefix")
                        except (OSError, RuntimeError, subprocess.SubprocessError):
                            state["phase"] = "send_error"
                            record("error", "AI 답변 전송을 확인하지 못했어요.")
                        else:
                            state["sent_count"] += 1
                            state["last_send"] = now().isoformat(timespec="seconds")
                            record("sent", "명시적 호출에 답했어요." if explicit else "대화에 참여했어요.")
                    else:
                        record("watch", "AI 답변을 브라우저에 표시했어요.")
                    if state["phase"] != "send_error":
                        state["phase"] = "watching"
        wake.wait(max(0, options["poll_seconds"] - (time.monotonic() - tick)))
        wake.clear()


TEST_TEXT = "새 게임 이번 주에 나오는 거 맞아?\n응, 금요일 출시래. 콘솔에 사전 다운로드도 열렸대."


def sample_messages(sample):
    return [{"speaker": f"예시 {index}", "text": text, "new": True, "explicit": bool(CALL.search(text))} for index, text in enumerate(sample.splitlines(), 1) if text.strip()]


def test_api():
    with lock:
        options = settings.copy()
    try:
        answers = classify(sample_messages(TEST_TEXT), options)
    except (OSError, ValueError, subprocess.SubprocessError):
        with lock:
            state["api_test"] = "error"
            record("error", "예시 대화로 모델에 연결하지 못했어요.")
    else:
        with lock:
            state["api_test"] = "ok"
            record("watch", f"API 테스트 성공 · 질문 {len(answers)}개 응답")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def deliver(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, data):
        self.deliver(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def valid_host(self):
        return self.headers.get("Host") == f"{HOST}:{PORT}"

    def do_GET(self):
        if not self.valid_host():
            self.json(403, {"error": "Forbidden"})
        elif self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            self.deliver(200, (ROOT / filename).read_bytes(), mime)
        elif self.path == "/api/status":
            with lock:
                data = dict(state, events=list(events), room=ROOM, model=decisions.MODEL)
                data.update(engine="responses", reasoning_effort=decisions.REASONING_EFFORT)
                data["next_run"] = state["next_run"].isoformat() if state["next_run"] else None
                data.update(interval_minutes=settings["interval_minutes"], context_minutes=settings["context_minutes"], poll_seconds=settings["poll_seconds"], quiet_seconds=settings["quiet_seconds"], assistant_mode=any(q["type"] == "assistant" for q in settings["questions"]), post_to_chat=settings["post_to_chat"])
            self.json(200, data)
        elif self.path == "/api/settings":
            with lock:
                data = {"settings": settings.copy(), "defaults": DEFAULT_SETTINGS, "model": decisions.MODEL,
                        "reasoning_effort": decisions.REASONING_EFFORT}
            self.json(200, data)
        else:
            self.json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.valid_host() or self.headers.get("Origin") != ORIGIN or self.headers.get("X-Kakao-Console") != "1":
            self.json(403, {"error": "Forbidden"})
        elif self.path == "/api/test":
            with lock:
                start_test = state["api_test"] != "running"
                if start_test:
                    state["api_test"] = "running"
            if start_test:
                threading.Thread(target=test_api, daemon=True).start()
            self.json(202, {"ok": True})
        elif self.path in {"/api/start", "/api/pause"}:
            set_enabled(self.path == "/api/start")
            self.json(200, {"ok": True})
        elif self.path in {"/api/settings", "/api/settings/test"}:
            self.post_settings()
        else:
            self.json(404, {"error": "Not found"})

    def post_settings(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                raise ValueError("설정 크기를 확인해 주세요.")
            data = json.loads(self.rfile.read(length))
            options = validated_settings(data)
            sample = data.get("sample", TEST_TEXT)
            if self.path == "/api/settings/test" and (
                not isinstance(sample, str) or not sample.strip() or len(sample) > 4000
            ):
                raise ValueError("테스트 문장을 1–4000자로 입력해 주세요.")
        except (ValueError, UnicodeError, RecursionError) as error:
            message = "설정 JSON 형식을 확인해 주세요." if isinstance(error, (json.JSONDecodeError, UnicodeError, RecursionError)) else str(error)
            self.json(400, {"error": message})
            return
        if self.path == "/api/settings":
            try:
                save_settings(options)
            except OSError:
                self.json(500, {"error": "설정을 이 Mac에 저장하지 못했어요."})
            else:
                self.json(200, {"ok": True})
            return
        try:
            answers = classify(sample_messages(sample), options)
        except urllib.error.HTTPError as error:
            self.json(502, {"error": f"Responses API가 요청을 받지 못했어요. (HTTP {error.code})"})
        except (OSError, ValueError, subprocess.SubprocessError):
            self.json(502, {"error": "모델 응답을 받지 못했어요. API 키와 연결을 확인해 주세요."})
        else:
            self.json(200, {"answers": answers, "preview": render_answers(answers) or "공유할 중요 내용이 없어요."})


def load_pending():
    if PENDING_FILE.exists():
        pending_windows.extend((datetime.fromisoformat(end), datetime.fromisoformat(start))
                               for end, start in json.loads(PENDING_FILE.read_text()))
        state["pending_count"] = len(pending_windows)
    elif LEGACY_PENDING_FILE.exists():
        ends = (datetime.fromisoformat(item) for item in json.loads(LEGACY_PENDING_FILE.read_text()))
        store_pending((end, end - timedelta(hours=1)) for end in ends)
    if RECOVERY_FILE.exists():
        requested = datetime.fromisoformat(RECOVERY_FILE.read_text().strip()).astimezone(TZ)
        RECOVERY_FILE.unlink()
        if now() - timedelta(hours=1) < requested <= now():
            store_pending([*pending_windows, (requested, requested - timedelta(hours=1))])


def main():
    if not ROOM:
        raise SystemExit("KAKAO_ROOM 환경 변수에 대상 단톡방 이름을 지정해 주세요.")
    os.umask(0o077)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    load_pending()
    if HISTORY_FILE.exists():
        try:
            digest_history.extend(json.loads(HISTORY_FILE.read_text()))
        except (OSError, ValueError):
            record("error", "이전 요약 기록을 불러오지 못했어요.")
    if SETTINGS_FILE.exists():
        try:
            settings.update(validated_settings(json.loads(SETTINGS_FILE.read_text())))
        except (OSError, ValueError):
            record("error", "저장된 설정을 불러오지 못해 기본값으로 실행했어요.")
    enabled = RUNTIME / "enabled"
    if enabled.exists() and enabled.read_text().strip() == "1":
        set_enabled(True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=monitor, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
