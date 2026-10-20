// 导入页面交互：上传 → 建任务 → 轮询进度。所有文本均以 textContent 写入。
const API = "/api";

const STEPS = [
  ["entry", "入口校验"],
  ["save_file", "归档原文件"],
  ["parse_file", "解析文件"],
  ["extract_metadata", "识别归属"],
  ["build_records", "构建记录"],
  ["embedding", "向量化"],
  ["persist", "写入知识库"],
  ["finish", "发布成功清单"],
];

const $ = (id) => document.getElementById(id);
const state = {
  file: null,
  taskId: null,
  timer: null,
  controller: null,
  polling: false,
};

// ---- 健康检查 ----------------------------------------------------------
async function loadHealth() {
  const dot = $("health-dot");
  const text = $("health-text");
  try {
    const res = await fetch(`${API}/health`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    const ready = data.minio === "configured" && data.milvus === "configured";
    dot.className = `dot ${ready ? "ok" : "warn"}`;
    text.textContent = `MinIO ${label(data.minio)} · Milvus ${label(data.milvus)}`;
  } catch {
    dot.className = "dot warn";
    text.textContent = "无法连接服务";
  }
}

function label(value) {
  return value === "configured" ? "已配置" : "未配置";
}

// ---- 文件选择 ----------------------------------------------------------
function selectFile(file) {
  if (!file) return;
  state.file = file;
  $("file-name").textContent = `${file.name} · ${formatSize(file.size)}`;
  $("submit").disabled = false;
  hideError();
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function hideError() {
  const box = $("form-error");
  box.hidden = true;
  box.textContent = "";
}

function showError(message) {
  const box = $("form-error");
  box.textContent = message;
  box.hidden = false;
}

// ---- 提交 --------------------------------------------------------------
async function submit() {
  if (!state.file) {
    showError("请先选择文件");
    return;
  }
  const contentType = $("content_type").value;
  if (!contentType) {
    showError("请选择内容类型");
    return;
  }
  hideError();
  $("submit").disabled = true;
  $("submit").textContent = "上传中…";

  const form = new FormData();
  form.append("file", state.file);
  form.append("content_type", contentType);
  for (const key of ["series_name", "course_name", "project_name", "question_bank_name"]) {
    const value = $(key).value.trim();
    if (value) form.append(key, value);
  }

  try {
    const res = await fetch(`${API}/import/tasks`, { method: "POST", body: form });
    const data = await readJson(res);
    if (!res.ok) throw new ApiError(data.code, data.message, res.status);

    state.taskId = data.task_id;
    $("progress-card").hidden = false;
    $("ids").textContent = `task_id ${data.task_id} · document_id ${data.document_id}`;
    $("submit").textContent = "导入中…";
    startPolling();
  } catch (err) {
    showError(describe(err));
    $("submit").disabled = false;
    $("submit").textContent = "开始导入";
  }
}

class ApiError extends Error {
  constructor(code, message, status) {
    super(message || "请求失败");
    this.code = code || "UNKNOWN";
    this.status = status || 0;
  }
}

async function readJson(res) {
  try {
    return await res.json();
  } catch {
    return { code: "INVALID_RESPONSE", message: `服务返回异常（HTTP ${res.status}）` };
  }
}

function describe(err) {
  if (!(err instanceof ApiError)) return `请求失败：${err.message}`;
  if (err.status === 413 || err.code === "FILE_TOO_LARGE") return "文件超过 100 MB 上限";
  if (err.code === "UNSUPPORTED_FILE_FORMAT") return "只支持 .md / .docx / .pdf 文件";
  if (err.code === "EMPTY_UPLOAD_FILE") return "文件为空";
  if (err.status === 503) return `依赖服务不可用：${err.message}`;
  if (err.status === 422) return "请求参数不合法";
  return `${err.message}（${err.code}）`;
}

// ---- 轮询 --------------------------------------------------------------
function startPolling() {
  state.polling = true;
  clearTimeout(state.timer);
  poll();
}

async function poll() {
  if (!state.polling || !state.taskId) return;
  state.controller = new AbortController();
  try {
    const res = await fetch(`${API}/import/tasks/${state.taskId}`, { signal: state.controller.signal });
    const data = await readJson(res);
    if (!res.ok) throw new ApiError(data.code, data.message, res.status);

    render(data);
    if (data.status === "succeeded" || data.status === "failed") {
      stopPolling();
      $("submit").disabled = false;
      $("submit").textContent = "再导入一个";
      return;
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    stopPolling();
    showError(describe(err));
    $("submit").disabled = false;
    $("submit").textContent = "开始导入";
    return;
  }
  state.timer = setTimeout(poll, 1500);
}

function stopPolling() {
  state.polling = false;
  clearTimeout(state.timer);
}

// ---- 渲染 --------------------------------------------------------------
function render(task) {
  const badge = $("status-badge");
  badge.className = `badge ${task.status}`;
  badge.textContent = { pending: "等待中", running: "执行中", succeeded: "已完成", failed: "失败" }[task.status] || task.status;

  renderStepper(task);
  renderCounts(task.counts || {});
  renderWarnings((task.result && task.result.warnings) || []);
  renderOutcome(task);
}

function renderStepper(task) {
  const completed = new Set(task.completed_nodes || []);
  const running = new Set(task.running_nodes || []);
  const failed = new Set(task.failed_nodes || []);
  const list = $("stepper");
  list.textContent = "";

  for (const [key, title] of STEPS) {
    const li = document.createElement("li");
    let status = "pending";
    if (completed.has(key)) status = "completed";
    if (running.has(key)) status = "running";
    if (failed.has(key)) status = "failed";
    li.className = status;

    const marker = document.createElement("span");
    marker.className = "marker";
    marker.textContent = status === "completed" ? "✓" : status === "failed" ? "!" : status === "running" ? "…" : "";
    li.appendChild(marker);
    li.appendChild(document.createTextNode(`${title} · ${key}`));
    list.appendChild(li);
  }

  if (failed.has("handle_error")) {
    const li = document.createElement("li");
    li.className = "failed";
    const marker = document.createElement("span");
    marker.className = "marker";
    marker.textContent = "!";
    li.appendChild(marker);
    li.appendChild(document.createTextNode("错误处理 · handle_error"));
    list.appendChild(li);
  }
}

function sum(object) {
  return Object.values(object || {}).reduce((total, value) => total + (Number(value) || 0), 0);
}

function renderCounts(counts) {
  const box = $("counts");
  box.textContent = "";
  const items = [
    ["文件", counts.files],
    ["有效记录", counts.records],
    ["拒绝记录", counts.rejected_records],
    ["检索片段", sum(counts.chunks_by_collection)],
    ["已向量化", sum(counts.embedded_by_collection)],
    ["已入库", counts.persisted_records],
  ];
  for (const [label, value] of items) {
    const cell = document.createElement("div");
    cell.className = "count";
    const name = document.createElement("span");
    name.textContent = label;
    const num = document.createElement("strong");
    num.textContent = Number(value || 0).toLocaleString("zh-CN");
    cell.append(name, num);
    box.appendChild(cell);
  }
}

function renderWarnings(warnings) {
  const block = $("warn-block");
  const list = $("warnings");
  list.textContent = "";
  if (!warnings.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  for (const warning of warnings) {
    const li = document.createElement("li");
    li.textContent = `${warning.code || "WARNING"}：${warning.message || ""}`;
    list.appendChild(li);
  }
}

function renderOutcome(task) {
  const errorBox = $("error-block");
  const okBox = $("ok-block");
  const result = task.result || {};

  if (task.status === "failed") {
    const parts = [];
    if (result.failed_node) parts.push(`失败节点：${result.failed_node}`);
    if (result.code) parts.push(`错误码：${result.code}`);
    parts.push(result.message || "任务失败，请通过 task_id 核对日志");
    $("error-text").textContent = parts.join(" · ");
    errorBox.hidden = false;
    okBox.hidden = true;
    return;
  }

  if (task.status === "succeeded") {
    const collections = Object.entries(result.collections || {})
      .map(([name, count]) => `${name}:${count}`)
      .join(" · ");
    $("ok-text").textContent =
      `记录 ${result.records ?? 0} 条，写入 ${result.total_rows ?? 0} 行（${collections || "无"}）；` +
      `清单 ${result.manifest_key || "-"}`;
    okBox.hidden = false;
    errorBox.hidden = true;
    return;
  }

  errorBox.hidden = true;
  okBox.hidden = true;
}

// ---- 事件绑定 ----------------------------------------------------------
function bind() {
  const input = $("file");
  const zone = $("dropzone");

  input.addEventListener("change", () => selectFile(input.files[0]));

  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
    });
  }
  zone.addEventListener("drop", (event) => {
    const file = event.dataTransfer && event.dataTransfer.files[0];
    selectFile(file);
  });

  $("submit").addEventListener("click", submit);

  window.addEventListener("beforeunload", () => {
    stopPolling();
    if (state.controller) state.controller.abort();
  });
}

bind();
loadHealth();
