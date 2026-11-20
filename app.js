import { initSettings } from "/settings.js";

const byId = (id) => document.getElementById(id);
const toggle = byId("toggle");
const status = byId("status");
const test = byId("api-test");
const list = byId("activity-list");
const clock = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const hourClock = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const phases = {
  paused: ["일시 정지", "다시 켜면 설정한 간격에 맞춰 판독해요.", "idle"],
  watching: ["다음 요약 대기 중", "새롭게 공유할 중요 내용이 있을 때만 요약을 보내요.", "active"],
  window_wait: ["카톡 창 접근 대기 중", "카톡 창을 읽을 수 있게 되면 대기 중인 시간대의 판독을 이어서 처리해요.", "idle"],
  generating: ["답변 생성 중", "AI가 최근 맥락과 이전 대화를 참고하고 있어요.", "active"],
  access_error: ["손쉬운 사용 권한 필요", "이 Mac에서 백그라운드 카톡 접근 권한을 허용해야 해요.", "error"],
  room_error: ["단톡방 연결 필요", "Mac 카카오톡에서 설정한 단톡방을 열어 주세요.", "error"],
  model_error: ["판독 실패", "API 키와 OpenAI API 연결을 확인해 주세요.", "error"],
  send_error: ["전송 확인 실패", "실패한 알림은 자동으로 다시 보내지 않아요.", "error"],
};
let enabled = false;
let updating = false;
let available = false;
let eventSignature = "";

const write = (element, value) => {
  if (element.textContent !== String(value)) element.textContent = value;
};

function setStatus(label, detail, kind) {
  write(status, label);
  status.dataset.kind = kind;
  write(byId("detail"), detail);
}

function draw(data) {
  write(byId("page-title"), data.room || "단톡방 AI 어시스턴트");
  available = true;
  enabled = data.enabled;
  toggle.disabled = updating;
  test.disabled = data.api_test === "running";
  write(test, test.disabled ? "API 확인 중" : "API 테스트");
  write(toggle, enabled ? "어시스턴트 일시 정지" : "어시스턴트 켜기");
  setStatus(...(phases[data.phase] ?? phases.paused));
  if (data.phase === "window_wait" && data.pending_count) {
    write(byId("detail"), `대기 중 ${data.pending_count}건. 카톡 창을 읽을 수 있게 되면 자동으로 다시 시도해요.`);
  }
  write(byId("next-run"), data.next_run ? hourClock.format(new Date(data.next_run)) : "—");
  write(byId("interval-label"), data.assistant_mode ? `${data.poll_seconds}초 간격 탐지` : `한국 시간 · ${data.interval_minutes}분 간격`);
  write(byId("schedule-summary"), data.assistant_mode ? `${data.poll_seconds}초마다 확인 · AI야, @AI로 호출하면 즉답 · 최근 ${data.context_minutes}분 맥락` : `${data.interval_minutes}분마다 최근 ${data.context_minutes}분의 맥락을 참고해 새 중요 내용을 찾아요.`);
  if (data.assistant_mode && data.phase === "watching") setStatus("새 메시지 감지 중", "명시적으로 호출하면 바로 답하고 일반 대화는 필요한 경우에만 참여해요.", "active");
  write(byId("delivery-label"), data.post_to_chat ? "카카오톡 · 이번 실행" : "브라우저에만 표시");
  const answers = data.latest?.answers ?? [];
  const first = answers[0];
  const label = first?.choice ?? first?.probability ?? first?.score ?? (first?.type === "refusal" ? "판정 보류" : "—");
  write(byId("topic"), first?.type === "assistant" ? (first.reply ? "AI 답변 생성" : "참여 생략") : first?.type === "digest" ? (first.items.length ? `중요 내용 ${first.items.length}건` : "새 중요 내용 없음") : label);
  write(byId("tone"), first?.type === "assistant" ? (first.reply ? "최근 응답" : "새 메시지 대기") : first?.type === "digest" ? (first.items[0]?.category ?? "전송 생략") : first ? `${first.name} · 질문 ${answers.length}개 응답` : "아직 판독 전");
  byId("latest-result").hidden = !answers.length;
  byId("latest-message").hidden = !data.latest?.notice;
  if (data.latest?.notice) write(byId("latest-notice"), data.latest.notice);
  if (answers.length) write(byId("latest-json"), JSON.stringify({ answers }, null, 2));
  write(byId("sent"), data.sent_count);
  write(byId("memory-status"), data.assistant_mode ? `이 Mac에 최근 대화 ${data.memory_count ?? 0}건 저장 · 최대 24시간 / 2,000건` : "주기 요약 모드");
  write(byId("last-check"), data.last_check
    ? `최근 확인 ${clock.format(new Date(data.last_check))}`
    : "아직 확인 전");

  const signature = JSON.stringify(data.events);
  if (signature === eventSignature) return;
  eventSignature = signature;
  const rows = data.events.map((event) => {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = event.detail;
    const time = document.createElement("time");
    time.dateTime = event.time;
    time.textContent = clock.format(new Date(event.time));
    row.append(label, time);
    return row;
  });
  if (!rows.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "아직 기록된 활동이 없어요.";
    rows.push(empty);
  }
  list.replaceChildren(...rows);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { signal: AbortSignal.timeout(4500) });
    if (!response.ok) throw new Error("status unavailable");
    draw(await response.json());
  } catch {
    available = false;
    toggle.disabled = true;
    test.disabled = true;
    setStatus("연결 끊김", "이 Mac에서 주제 판독기가 실행 중인지 확인해 주세요.", "error");
  }
}

toggle.addEventListener("click", async () => {
  if (!available || updating) return;
  updating = true;
  toggle.disabled = true;
  try {
    const response = await fetch(enabled ? "/api/pause" : "/api/start", {
      method: "POST", headers: { "X-Kakao-Console": "1" }, signal: AbortSignal.timeout(35000),
    });
    if (!response.ok) throw new Error("change rejected");
    await refresh();
  } catch {
    setStatus("설정 변경 실패", "주제 판독 상태를 다시 확인해 주세요.", "error");
  } finally {
    updating = false;
    toggle.disabled = !available;
  }
});

test.addEventListener("click", async () => {
  test.disabled = true;
  try {
    const response = await fetch("/api/test", {
      method: "POST", headers: { "X-Kakao-Console": "1" }, signal: AbortSignal.timeout(4500),
    });
    if (!response.ok) throw new Error("test rejected");
    await refresh();
  } catch {
    test.disabled = false;
    setStatus("API 테스트 시작 실패", "잠시 후 다시 시도해 주세요.", "error");
  }
});

async function poll() {
  if (!updating && !document.hidden) await refresh();
  setTimeout(poll, 2500);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !updating) void refresh();
});
void poll();
initSettings(refresh);
