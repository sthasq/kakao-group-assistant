const byId = (id) => document.getElementById(id);
const form = byId("settings-form");
const dialog = byId("settings-dialog");
const list = byId("question-list");
const open = byId("open-settings");
const preview = byId("settings-result");
const previewStatus = byId("preview-status");
const feedback = byId("settings-message");
const template = byId("question-template");
let defaults;

function showFeedback(message = "", error = false) {
  feedback.textContent = message;
  feedback.hidden = !message;
  feedback.dataset.error = String(error);
}

function numberQuestions() {
  [...list.children].forEach((card, index) => {
    card.querySelector("legend").textContent = `질문 ${index + 1}`;
    card.querySelector(".remove-question").setAttribute("aria-label", `질문 ${index + 1} 삭제`);
  });
  byId("add-question").disabled = list.children.length >= 20;
}

function addQuestion(question = { type: "predicate" }) {
  const card = template.content.firstElementChild.cloneNode(true);
  const type = card.querySelector(".question-type");
  const options = card.querySelector(".question-options");
  const values = options.querySelector("textarea");
  const rank = card.querySelector(".question-rank");
  const rankCount = card.querySelector(".question-rank-count");
  card.querySelector(".question-name").value = question.name ?? "";
  card.querySelector(".question-instructions").value = question.instructions ?? "";
  type.value = question.type;
  rankCount.value = question.max_items ?? question.rank_count ?? 3;
  values.value = (question.choices ?? question.levels ?? []).map((entry) => {
    const label = entry.value ?? entry.label;
    return entry.description ? `${label} | ${entry.description}` : label;
  }).join("\n");

  function syncType() {
    const isScale = type.value === "score";
    const isRanked = type.value === "ranked" || type.value === "digest";
    options.hidden = !["choice", "score"].includes(type.value);
    values.required = !options.hidden;
    values.disabled = options.hidden;
    rank.hidden = !isRanked;
    rankCount.required = isRanked;
    rankCount.disabled = !isRanked;
    rank.querySelector("small").textContent = type.value === "digest" ? "새로운 중요 내용만 정리해요. 없으면 전송하지 않아요." : "선지를 미리 정하지 않고 모델이 대화에서 직접 이름과 비중을 만들어요.";
    options.querySelector("span").textContent = isScale ? "척도 단계" : "선지";
    options.querySelector("small").textContent = isScale
      ? "낮은 단계부터 한 줄씩 입력하세요. 이름 뒤에 | 설명을 추가할 수 있어요."
      : "한 줄에 선지 하나씩 입력하세요. 이름 뒤에 | 설명을 추가할 수 있어요.";
  }
  type.addEventListener("change", syncType);
  card.querySelector(".remove-question").addEventListener("click", () => {
    card.remove();
    numberQuestions();
    byId("add-question").focus();
  });
  syncType();
  list.append(card);
  numberQuestions();
  return card;
}

function fill(settings) {
  form.elements.interval_minutes.value = settings.interval_minutes;
  form.elements.poll_seconds.value = settings.poll_seconds;
  form.elements.quiet_seconds.value = settings.quiet_seconds;
  form.elements.context_minutes.value = settings.context_minutes;
  form.elements.read_limit.value = settings.read_limit;
  form.elements.post_to_chat.checked = settings.post_to_chat;
  form.elements.full_answers.checked = settings.full_answers;
  form.elements.api_options.value = JSON.stringify(settings.api_options, null, 2);
  list.replaceChildren();
  settings.questions.forEach(addQuestion);
}

function serialize() {
  if (!form.reportValidity()) return null;
  if (!list.childElementCount) throw new Error("판정 질문을 한 개 이상 추가해 주세요.");
  let apiOptions;
  try {
    apiOptions = JSON.parse(form.elements.api_options.value.trim() || "{}");
  } catch {
    throw new Error("고급 요청 옵션의 JSON 형식을 확인해 주세요.");
  }
  const questions = [...list.children].map((card) => {
    const type = card.querySelector(".question-type").value;
    const question = {
      type,
      name: card.querySelector(".question-name").value.trim(),
      instructions: card.querySelector(".question-instructions").value.trim(),
    };
    if (type === "digest") {
      question.max_items = card.querySelector(".question-rank-count").valueAsNumber;
    } else if (type === "ranked") {
      question.rank_count = card.querySelector(".question-rank-count").valueAsNumber;
    } else if (type === "choice" || type === "score") {
      const key = type === "score" ? "label" : "value";
      question[type === "score" ? "levels" : "choices"] =
        card.querySelector(".question-options textarea").value.split("\n").filter((line) => line.trim()).map((line) => {
          const [label, ...rest] = line.split("|");
          const description = rest.join("|").trim();
          return { [key]: label.trim(), ...(description && { description }) };
        });
    }
    return question;
  });
  return {
    interval_minutes: form.elements.interval_minutes.valueAsNumber,
    poll_seconds: form.elements.poll_seconds.valueAsNumber,
    quiet_seconds: form.elements.quiet_seconds.valueAsNumber,
    context_minutes: form.elements.context_minutes.valueAsNumber,
    read_limit: form.elements.read_limit.valueAsNumber,
    post_to_chat: form.elements.post_to_chat.checked,
    full_answers: form.elements.full_answers.checked,
    questions,
    api_options: apiOptions,
  };
}

async function post(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Kakao-Console": "1" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(75000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? "요청을 처리하지 못했어요.");
  return data;
}

export function initSettings(refresh) {
  open.addEventListener("click", async () => {
    open.disabled = true;
    try {
      const response = await fetch("/api/settings", { signal: AbortSignal.timeout(4500) });
      if (!response.ok) throw new Error();
      const data = await response.json();
      defaults = data.defaults;
      fill(data.settings);
      byId("settings-model").textContent = `${data.model} / ${data.reasoning_effort}`;
      showFeedback();
      preview.hidden = true;
      byId("settings-json-details").hidden = true;
      previewStatus.textContent = "입력한 예시만 API로 보내요.";
      dialog.showModal();
    } catch {
      byId("settings-open-error").hidden = false;
    } finally {
      open.disabled = false;
      if (dialog.open) byId("settings-open-error").hidden = true;
    }
  });

  byId("close-settings").addEventListener("click", () => dialog.close());
  byId("add-question").addEventListener("click", () => addQuestion().querySelector("input").focus());
  byId("restore-settings").addEventListener("click", () => {
    fill(defaults);
    showFeedback("기본값을 불러왔어요. 저장하면 적용돼요.");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = byId("save-settings");
    showFeedback();
    button.disabled = true;
    try {
      const draft = serialize();
      if (!draft) return;
      await post("/api/settings", draft);
      dialog.close();
      await refresh();
    } catch (error) {
      showFeedback(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  byId("test-settings").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    preview.hidden = true;
    byId("settings-json-details").hidden = true;
    button.disabled = true;
    showFeedback();
    try {
      const draft = serialize();
      if (!draft) return;
      const sample = form.elements.sample.value.trim();
      previewStatus.textContent = "모델 응답을 기다리는 중이에요.";
      const result = await post("/api/settings/test", { ...draft, ...(sample && { sample }) });
      preview.textContent = result.preview;
      preview.hidden = false;
      byId("settings-json").textContent = JSON.stringify({ answers: result.answers }, null, 2);
      byId("settings-json-details").hidden = false;
      previewStatus.textContent = "테스트 완료. 결과를 단톡방에 보내지 않았어요.";
    } catch (error) {
      previewStatus.textContent = "테스트를 완료하지 못했어요.";
      showFeedback(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}
