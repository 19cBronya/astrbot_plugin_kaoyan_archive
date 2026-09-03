import {
  parseSummaryBlocks,
  renderDisplayMath,
  renderMath,
} from "./math-renderer.mjs";

const bridge = window.AstrBotPluginPage;

const state = {
  config: null,
  stats: null,
  questions: [],
  active: null,
};
const imagePreviewCache = new Map();
const previewableImageTypes = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "image/avif",
]);

const $ = (id) => document.getElementById(id);
const connection = $("connection");

function dateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(Number(value) * 1000));
}

function badge(label, type = "muted") {
  const span = document.createElement("span");
  span.className = `badge ${type}`;
  span.textContent = label;
  return span;
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.style.background = error ? "var(--danger)" : "#17201d";
  node.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.add("hidden"), 2600);
}

async function apiGet(endpoint, params = {}) {
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body = {}) {
  return bridge.apiPost(endpoint, body);
}

function renderStats() {
  const stats = state.stats || {};
  const items = [
    ["归档题目", stats.questions ?? 0],
    ["原始事件", stats.events ?? 0],
    ["归档科目", stats.subjects?.length ?? 0],
    ["正在整理", stats.finalizing ?? 0],
    ["整理失败", stats.failed ?? 0],
  ];
  const container = $("stats");
  container.replaceChildren();
  for (const [label, value] of items) {
    const card = document.createElement("div");
    card.className = "stat";
    const caption = document.createElement("span");
    caption.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    card.append(caption, strong);
    container.append(card);
  }
}

function renderConfig() {
  const config = state.config || {};
  fillSelect($("filter-subject"), "全部科目", config.subjects || []);
  const umos = new Set([...(state.stats?.umo_values || []), ...(config.umo_whitelist || [])]);
  for (const question of state.questions) {
    if (question.umo) umos.add(question.umo);
  }
  fillSelect($("filter-umo"), "全部会话", [...umos]);
}

function fillSelect(select, emptyLabel, values) {
  const selected = select.value;
  select.replaceChildren(new Option(emptyLabel, ""));
  for (const value of values) {
    select.add(new Option(value, value));
  }
  select.value = selected;
}

function statusType(status) {
  if (status === "ARCHIVED") return "ok";
  if (status === "FINALIZE_FAILED") return "error";
  if (status === "FINALIZING") return "warn";
  return "muted";
}

function statusLabel(status) {
  return {
    ARCHIVED: "已归档",
    FINALIZING: "整理中",
    FINALIZE_FAILED: "整理失败",
  }[status] || status || "未知";
}

function renderQuestions() {
  const list = $("question-list");
  list.replaceChildren();
  $("result-count").textContent = `${state.questions.length} 条结果`;
  $("empty-list").classList.toggle("hidden", state.questions.length > 0);
  for (const item of state.questions) {
    const card = document.createElement("article");
    card.className = `question${item.deleted_at ? " deleted" : ""}`;
    card.tabIndex = 0;

    const main = document.createElement("div");
    const id = document.createElement("p");
    id.className = "question-id";
    id.textContent = item.public_id || "等待编号";
    const title = document.createElement("h3");
    title.textContent = item.title || "正在整理题目";
    const meta = document.createElement("div");
    meta.className = "question-meta";
    for (const value of [item.subject || "待分类", item.umo, `${item.event_count || 0} 条消息`, dateTime(item.archived_at || item.created_at)]) {
      const span = document.createElement("span");
      span.textContent = value;
      meta.append(span);
    }
    main.append(id, title, meta);
    const knowledge = document.createElement("div");
    knowledge.className = "question-knowledge";
    for (const point of (item.knowledge_points || []).slice(0, 5)) {
      knowledge.append(knowledgeChip(point));
    }
    if (knowledge.childElementCount) main.append(knowledge);

    const status = document.createElement("div");
    status.className = "question-status";
    status.append(badge(item.deleted_at ? "已删除" : statusLabel(item.status), item.deleted_at ? "error" : statusType(item.status)));
    const arrow = document.createElement("span");
    arrow.className = "question-arrow";
    arrow.textContent = "›";
    card.append(main, status, arrow);
    card.addEventListener("click", () => openDetail(item.uuid));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") openDetail(item.uuid);
    });
    list.append(card);
  }
  renderMath(list);
}

function renderDetail(detail) {
  state.active = detail;
  $("detail-id").textContent = detail.public_id || detail.uuid;
  $("detail-title").textContent = detail.title || "未命名题目";
  renderSummary(detail.summary || detail.error || "暂无总结");
  renderKnowledge(detail);
  $("detail-event-count").textContent = `${detail.events?.length || 0} 条`;

  const meta = $("detail-meta");
  meta.replaceChildren(
    badge(detail.subject || "待分类", "ok"),
    badge(statusLabel(detail.status), statusType(detail.status)),
    badge(detail.umo),
    badge(dateTime(detail.archived_at || detail.created_at)),
  );
  if (detail.analysis_warning) meta.append(badge(detail.analysis_warning, "warn"));

  const timeline = $("detail-timeline");
  timeline.replaceChildren();
  for (const event of detail.events || []) {
    const item = document.createElement("article");
    item.className = `event ${event.direction}${String(event.relation).includes("boundary") ? " boundary" : ""}`;
    const head = document.createElement("div");
    head.className = "event-head";
    const who = document.createElement("span");
    who.textContent = `${event.direction === "user" ? "用户" : event.direction === "assistant" ? "助手" : "控制"} · ${relationLabel(event.relation)}`;
    const when = document.createElement("span");
    when.textContent = dateTime(event.created_at);
    head.append(who, when);
    const body = document.createElement("p");
    body.className = "event-body";
    body.textContent = event.text || (event.attachments?.length ? "[附件消息]" : "");
    item.append(head, body);
    for (const attachment of event.attachments || []) {
      item.append(renderAttachment(attachment));
    }
    timeline.append(item);
  }

  const actions = $("detail-actions");
  actions.replaceChildren();
  if (detail.status === "FINALIZE_FAILED") {
    const retry = actionButton("重新整理", "primary", () => actOnQuestion("retry"));
    actions.append(retry);
  }
  if (detail.deleted_at) {
    actions.append(actionButton("恢复题目", "primary", () => actOnQuestion("restore")));
  } else {
    actions.append(actionButton("软删除", "danger", () => actOnQuestion("delete")));
  }
  renderMath($("detail-overlay"));
}

function renderAttachment(attachment) {
  const mimeType = String(attachment.mime_type || "").toLowerCase();
  const label = `${attachment.name || "图片附件"} · ${Math.ceil((attachment.size || 0) / 1024)} KiB`;
  if (!previewableImageTypes.has(mimeType) || !attachment.sha256) {
    const tag = document.createElement("span");
    tag.className = "attachment";
    tag.textContent = label;
    return tag;
  }

  const figure = document.createElement("figure");
  figure.className = "attachment-preview";
  const frame = document.createElement("button");
  frame.type = "button";
  frame.className = "attachment-image-frame";
  frame.title = "点击切换原图大小";
  const loading = document.createElement("span");
  loading.className = "attachment-loading";
  loading.textContent = "图片加载中…";
  const image = document.createElement("img");
  image.className = "attachment-image hidden";
  image.alt = attachment.name || "题目图片";
  image.loading = "lazy";
  image.decoding = "async";
  const caption = document.createElement("figcaption");
  caption.textContent = label;
  frame.append(loading, image);
  figure.append(frame, caption);

  frame.addEventListener("click", () => figure.classList.toggle("expanded"));
  loadImagePreview(attachment.sha256)
    .then((dataUrl) => {
      image.addEventListener("load", () => {
        loading.classList.add("hidden");
        image.classList.remove("hidden");
      }, { once: true });
      image.addEventListener("error", () => {
        loading.textContent = "图片解码失败，已保留附件记录";
        loading.classList.add("error");
      }, { once: true });
      image.src = dataUrl;
    })
    .catch((error) => {
      loading.textContent = `图片加载失败：${error.message || "未知错误"}`;
      loading.classList.add("error");
    });
  return figure;
}

function loadImagePreview(sha256) {
  if (!imagePreviewCache.has(sha256)) {
    const pending = apiGet(`attachments/${encodeURIComponent(sha256)}`).then((result) => {
      if (!result?.data_url) throw new Error("预览接口未返回图片");
      return result.data_url;
    });
    imagePreviewCache.set(sha256, pending);
    pending.catch(() => imagePreviewCache.delete(sha256));
  }
  return imagePreviewCache.get(sha256);
}

function knowledgeChip(label) {
  const chip = document.createElement("span");
  chip.className = "knowledge-chip";
  chip.textContent = label;
  return chip;
}

function renderKnowledge(detail) {
  const container = $("detail-knowledge");
  container.replaceChildren();
  const points = [...(detail.knowledge_points || [])];
  if (!points.length) {
    for (const line of String(detail.summary || "").split(/\r?\n/)) {
      const heading = line.match(/^#{1,4}\s+(.+)/)?.[1]?.trim();
      if (heading && !/^(对话归档|题目|关键追问|解答结论|仍未解决)/.test(heading)) {
        points.push(heading);
      }
    }
  }
  if (!points.length && detail.subject) points.push(detail.subject);
  if (!points.length) {
    const empty = document.createElement("span");
    empty.className = "knowledge-empty";
    empty.textContent = "该题暂未提取知识点，可重新整理生成。";
    container.append(empty);
    return;
  }
  for (const point of [...new Set(points)].slice(0, 12)) {
    container.append(knowledgeChip(point));
  }
}

function renderSummary(markdown) {
  const container = $("detail-summary");
  container.replaceChildren();
  for (const block of parseSummaryBlocks(markdown)) {
    if (block.type === "math") {
      const node = document.createElement("div");
      renderDisplayMath(node, block.expression, block.source);
      container.append(node);
    } else if (block.type === "heading") {
      const node = document.createElement(`h${block.level}`);
      node.textContent = block.text;
      container.append(node);
    } else if (block.type === "list") {
      const list = document.createElement("ul");
      for (const text of block.items) {
        const item = document.createElement("li");
        item.textContent = text;
        list.append(item);
      }
      container.append(list);
    } else {
      const paragraph = document.createElement("p");
      paragraph.textContent = block.text;
      container.append(paragraph);
    }
  }
  renderMath(container);
}

function relationLabel(relation) {
  return String(relation || "")
    .split(",")
    .map((value) => ({
      primary: "题目正文",
      answer: "回答",
      boundary: "结束边界",
      excluded: "已排除",
      supplement: "补充",
      query: "查询",
      edit: "修改",
      reference: "引用",
    })[value] || value)
    .join(" / ");
}

function actionButton(label, kind, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${kind}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

async function loadOverview() {
  connection.textContent = "正在同步";
  connection.className = "badge muted";
  try {
    const [config, stats] = await Promise.all([apiGet("config"), apiGet("stats")]);
    state.config = config;
    state.stats = stats;
    renderConfig();
    renderStats();
    connection.textContent = "已连接";
    connection.className = "badge ok";
  } catch (error) {
    connection.textContent = "连接失败";
    connection.className = "badge error";
    toast(error.message || "无法连接插件 API", true);
  }
}

async function loadQuestions() {
  const params = {
    search: $("search").value.trim(),
    umo: $("filter-umo").value,
    subject: $("filter-subject").value,
    status: $("filter-status").value,
    include_deleted: $("include-deleted").checked ? "1" : "0",
    limit: 100,
  };
  try {
    const result = await apiGet("questions", params);
    state.questions = result.items || [];
    renderConfig();
    renderQuestions();
  } catch (error) {
    toast(error.message || "题目查询失败", true);
  }
}

async function openDetail(uuid) {
  try {
    const detail = await apiGet(`questions/${encodeURIComponent(uuid)}`);
    renderDetail(detail);
    $("detail-overlay").classList.remove("hidden");
    document.body.classList.add("dialog-open");
    $("close-detail").focus();
  } catch (error) {
    toast(error.message || "详情加载失败", true);
  }
}

async function actOnQuestion(action) {
  if (!state.active) return;
  if (action === "delete" && !window.confirm(`确认软删除 ${state.active.public_id || "这道题"}？原始记录仍会保留。`)) return;
  try {
    await apiPost(`questions/${encodeURIComponent(state.active.uuid)}/action`, { action });
    toast(action === "retry" ? "已提交重新整理" : "操作成功");
    closeDetail();
    await Promise.all([loadOverview(), loadQuestions()]);
  } catch (error) {
    toast(error.message || "操作失败", true);
  }
}

$("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  loadQuestions();
});
$("filters").addEventListener("change", (event) => {
  if (event.target.matches("select, input[type='checkbox']")) loadQuestions();
});
$("refresh").addEventListener("click", () => Promise.all([loadOverview(), loadQuestions()]));
function closeDetail() {
  $("detail-overlay").classList.add("hidden");
  document.body.classList.remove("dialog-open");
}
$("close-detail").addEventListener("click", closeDetail);
$("detail-overlay").addEventListener("click", (event) => {
  if (event.target === $("detail-overlay")) closeDetail();
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDetail();
});

await bridge.ready();
await loadOverview();
await loadQuestions();
