const bridge = window.AstrBotPluginPage;

const state = {
  config: null,
  stats: null,
  questions: [],
  active: null,
};

const $ = (id) => document.getElementById(id);
const connection = $("connection");

function text(value) {
  return value == null ? "" : String(value);
}

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
    ["白名单会话", state.config?.umo_whitelist?.length ?? 0],
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
  $("whitelist").value = (config.umo_whitelist || []).join("\n");
  $("whitelist-count").textContent = (config.umo_whitelist || []).length;
  $("classifier-mode").textContent = config.classifier_mode || "—";
  $("framework-commands").textContent = (config.framework_commands || []).join("、") || "—";
  $("subjects").textContent = (config.subjects || []).join("、") || "—";
  const select = $("filter-subject");
  const selected = select.value;
  select.replaceChildren(new Option("全部科目", ""));
  for (const subject of config.subjects || []) {
    select.add(new Option(subject, subject));
  }
  select.value = selected;
  renderRoutingStatus(config.routing_status || []);
}

function renderRoutingStatus(statuses) {
  const container = $("routing-status");
  container.replaceChildren();
  for (const status of statuses) {
    const item = document.createElement("div");
    item.className = `routing-item ${status.handler_enabled ? "ok" : "error"}`;
    const title = document.createElement("strong");
    title.textContent = status.handler_enabled ? "路由已启用插件" : "路由未启用插件";
    const detail = document.createElement("span");
    const profile = status.config_name || status.config_id || "当前配置";
    detail.textContent = status.handler_enabled
      ? `${profile} · ${status.umo}`
      : status.warning || `${profile} 未包含本插件`;
    item.append(title, detail);
    container.append(item);
  }
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

    const status = document.createElement("div");
    status.className = "question-status";
    status.append(badge(item.deleted_at ? "已删除" : statusLabel(item.status), item.deleted_at ? "error" : statusType(item.status)));
    card.append(main, status);
    card.addEventListener("click", () => openDetail(item.uuid));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") openDetail(item.uuid);
    });
    list.append(card);
  }
}

function renderDetail(detail) {
  state.active = detail;
  $("detail-id").textContent = detail.public_id || detail.uuid;
  $("detail-title").textContent = detail.title || "未命名题目";
  $("detail-summary").textContent = detail.summary || detail.error || "暂无摘要";
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
    item.className = `event ${event.direction}${event.relation === "boundary" ? " boundary" : ""}`;
    const head = document.createElement("div");
    head.className = "event-head";
    const who = document.createElement("span");
    who.textContent = `${event.direction === "user" ? "用户" : event.direction === "assistant" ? "助手" : "控制"} · ${event.relation}`;
    const when = document.createElement("span");
    when.textContent = dateTime(event.created_at);
    head.append(who, when);
    const body = document.createElement("p");
    body.className = "event-body";
    body.textContent = event.text || (event.attachments?.length ? "[附件消息]" : "");
    item.append(head, body);
    for (const attachment of event.attachments || []) {
      const tag = document.createElement("span");
      tag.className = "attachment";
      tag.textContent = `${attachment.name} · ${Math.ceil((attachment.size || 0) / 1024)} KiB`;
      item.append(tag);
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
    umo: $("filter-umo").value.trim(),
    subject: $("filter-subject").value,
    include_deleted: $("include-deleted").checked ? "1" : "0",
    limit: 100,
  };
  try {
    const result = await apiGet("questions", params);
    state.questions = result.items || [];
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
    $("detail-overlay").classList.add("hidden");
    await Promise.all([loadOverview(), loadQuestions()]);
  } catch (error) {
    toast(error.message || "操作失败", true);
  }
}

$("save-config").addEventListener("click", async () => {
  const button = $("save-config");
  const whitelist = $("whitelist").value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  button.disabled = true;
  $("save-state").textContent = "保存中…";
  try {
    const result = await apiPost("config", { umo_whitelist: whitelist });
    state.config.umo_whitelist = result.umo_whitelist;
    state.config.routing_status = result.routing_status || [];
    renderConfig();
    renderStats();
    $("save-state").textContent = "已保存";
    toast("UMO 白名单已更新");
  } catch (error) {
    $("save-state").textContent = "保存失败";
    toast(error.message || "保存失败", true);
  } finally {
    button.disabled = false;
  }
});

$("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  loadQuestions();
});
$("refresh").addEventListener("click", () => Promise.all([loadOverview(), loadQuestions()]));
$("close-detail").addEventListener("click", () => $("detail-overlay").classList.add("hidden"));
$("detail-overlay").addEventListener("click", (event) => {
  if (event.target === $("detail-overlay")) $("detail-overlay").classList.add("hidden");
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") $("detail-overlay").classList.add("hidden");
});

await bridge.ready();
await loadOverview();
await loadQuestions();
