"use strict";

/* ================================================================== */
/* 常量与状态                                                          */
/* ================================================================== */

const RISK = {
  readonly: { label: "只读", cls: "readonly" },
  low: { label: "低", cls: "low" },
  medium: { label: "中", cls: "medium" },
  high: { label: "高", cls: "high" },
};

const STATUS = {
  idle: { label: "空闲", cls: "idle" },
  starting: { label: "启动中", cls: "running" },
  running: { label: "运行中", cls: "running" },
  exited: { label: "已完成", cls: "ok" },
  failed: { label: "失败", cls: "fail" },
  timed_out: { label: "超时", cls: "timeout" },
  cancelled: { label: "已取消", cls: "cancel" },
  error: { label: "启动失败", cls: "fail" },
};

const RISK_FILTERS = [
  { id: "all", label: "全部" },
  { id: "readonly", label: "只读" },
  { id: "low", label: "低" },
  { id: "medium", label: "中" },
  { id: "high", label: "高" },
];

const state = {
  modules: [],
  errors: [],
  warnings: [],
  loadedAt: null,
  reloadCount: 0,
  modulesDir: "",
  activeModuleId: null,
  query: "",
  riskFilter: "all",
  selectedKey: null,
  params: {},
  previews: {},
  renderTimers: {},
  run: null,
  history: [],
  confirmCtx: null,
  confirmOpenedAt: 0,
  busy: false,
};

let toastTimer = null;
let searchTimer = null;
let elapsedTimer = null;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* ================================================================== */
/* 小工具                                                              */
/* ================================================================== */

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "attrs") {
      for (const [attr, attrValue] of Object.entries(value)) {
        if (attrValue == null || attrValue === false) continue;
        node.setAttribute(attr, attrValue === true ? "" : String(attrValue));
      }
    } else if (key === "on") {
      for (const [eventName, handler] of Object.entries(value)) {
        node.addEventListener(eventName, handler);
      }
    } else {
      node[key] = value;
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function keyOf(module, command) {
  return `${module.id}/${command.id}`;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 10000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function timeShort(ts) {
  const date = new Date(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

async function api(path, { method = "GET", body = null } = {}) {
  const headers = { "X-Requested-With": "cli-manager" };
  if (body != null) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body == null ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (err) {
    data = { error: text || `HTTP ${response.status}` };
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = data && data.code;
    error.details = data && data.details;
    throw error;
  }
  return data;
}

function showToast(message, kind = "ok", timeout = 2600) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast ${kind}`;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, timeout);
}

async function copyText(text, okMessage) {
  if (!text) {
    showToast("没有可复制的内容", "warn");
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const helper = document.createElement("textarea");
      helper.className = "copy-helper";
      helper.value = text;
      document.body.append(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
    showToast(okMessage || "已复制", "ok");
  } catch (err) {
    showToast("复制失败，请手动选中命令文本", "error");
  }
}

/* ================================================================== */
/* 数据加载                                                            */
/* ================================================================== */

async function pollHealth() {
  const pill = $("#health-pill");
  const text = $("#health-text");
  try {
    const data = await api("/api/health");
    pill.classList.remove("down");
    pill.classList.add("ok");
    text.textContent = `服务正常 · ${data.moduleCount} 模块`;
    pill.title = `端口 ${data.port} · pid ${data.pid} · 已运行 ${Math.round(data.uptimeSec)}s`;
  } catch (err) {
    pill.classList.remove("ok");
    pill.classList.add("down");
    text.textContent = "服务断线";
    pill.title = (err && err.message) || String(err);
  }
}

function applyModulesPayload(data) {
  state.modules = Array.isArray(data.modules) ? data.modules : [];
  state.errors = Array.isArray(data.errors) ? data.errors : [];
  state.warnings = Array.isArray(data.warnings) ? data.warnings : [];
  state.loadedAt = data.loadedAt || null;
  state.reloadCount = data.reloadCount || 0;
  state.modulesDir = data.modulesDir || "";

  const params = {};
  const previews = {};
  for (const module of state.modules) {
    for (const command of module.commands) {
      const key = keyOf(module, command);
      const previous = state.params[key] || {};
      const next = {};
      for (const param of command.params) {
        next[param.name] =
          typeof previous[param.name] === "string" ? previous[param.name] : param.default;
      }
      params[key] = next;
      const oldPreview = state.previews[key];
      previews[key] = oldPreview && !oldPreview.error
        ? oldPreview
        : { display: command.preview.display, argv: command.preview.argv, usesShell: command.preview.usesShell, error: "" };
    }
  }
  state.params = params;
  state.previews = previews;

  if (!state.modules.some((m) => m.id === state.activeModuleId)) {
    state.activeModuleId = state.modules.length ? state.modules[0].id : null;
  }
  state.selectedKey = null;
  renderAll();
}

async function loadModules() {
  const data = await api(`/api/modules?t=${Date.now()}`);
  applyModulesPayload(data);
  return data;
}

async function reloadConfigs() {
  try {
    const data = await api("/api/reload", { method: "POST", body: {} });
    applyModulesPayload(data);
    const total = state.modules.reduce((sum, m) => sum + m.commands.length, 0);
    showToast(`配置已重载：${state.modules.length} 个模块 / ${total} 条命令`, "ok");
    pollHealth();
  } catch (err) {
    showToast(`重载失败：${err.message}`, "error");
  }
}

/* ================================================================== */
/* 渲染：侧栏 / 筛选 / 提示                                             */
/* ================================================================== */

function activeModule() {
  return state.modules.find((m) => m.id === state.activeModuleId) || null;
}

function visibleItems() {
  const query = state.query.trim().toLowerCase();
  const modules = query
    ? state.modules
    : state.modules.filter((m) => m.id === state.activeModuleId);
  const items = [];
  for (const module of modules) {
    for (const command of module.commands) {
      if (state.riskFilter !== "all" && command.risk !== state.riskFilter) continue;
      if (query) {
        const preview = state.previews[keyOf(module, command)];
        const haystack = [
          module.name,
          command.name,
          command.description,
          command.id,
          (preview && preview.display) || "",
        ].join(" ").toLowerCase();
        if (!haystack.includes(query)) continue;
      }
      items.push({ module, command, key: keyOf(module, command) });
    }
  }
  return items;
}

function currentItem() {
  return visibleItems().find((item) => item.key === state.selectedKey) || null;
}

function renderSidebar() {
  const list = $("#module-list");
  list.replaceChildren();
  if (!state.modules.length) {
    list.append(h("p", { class: "empty" }, "没有可用模块，请检查 modules/ 目录"));
    return;
  }
  for (const module of state.modules) {
    const item = h(
      "button",
      {
        type: "button",
        class: "module-item" + (module.id === state.activeModuleId ? " active" : ""),
        title: module.description || module.name,
        on: {
          click: () => {
            state.activeModuleId = module.id;
            clearSearch();
            state.selectedKey = null;
            renderAll();
            $("#search").focus();
          },
        },
      },
      h("span", { class: "module-dot", attrs: { "aria-hidden": "true" } }),
      h(
        "span",
        { class: "module-text" },
        h("span", { class: "module-name" }, module.name),
        h("span", { class: "module-count" }, `${module.commands.length} 条命令`)
      )
    );
    if (!module.cwdExists) {
      item.append(h("span", { class: "module-warn", title: `工作目录不存在：${module.cwd}` }, "目录缺失"));
    }
    list.append(item);
  }
  const dirElement = $("#modules-dir");
  dirElement.textContent = state.modulesDir;
  dirElement.title = state.modulesDir;
}

function renderRiskFilters() {
  const box = $("#risk-filters");
  box.replaceChildren();
  for (const filter of RISK_FILTERS) {
    box.append(
      h(
        "button",
        {
          type: "button",
          class: "chip" + (state.riskFilter === filter.id ? " active" : ""),
          attrs: { "data-risk": filter.id },
          on: {
            click: () => {
              state.riskFilter = filter.id;
              state.selectedKey = null;
              renderRiskFilters();
              renderCommands();
            },
          },
        },
        filter.label
      )
    );
  }
}

function renderBanner() {
  const banner = $("#banner");
  const blocks = [];
  if (state.errors.length) {
    blocks.push(
      h(
        "div",
        {},
        h("strong", {}, `部分模块配置加载失败（${state.errors.length}），已跳过。`),
        h(
          "details",
          {},
          h("summary", {}, "查看错误详情"),
          h("pre", {}, state.errors.map((e) => `${e.source}: ${e.error}`).join("\n"))
        )
      )
    );
  }
  const module = activeModule();
  if (module && !module.cwdExists) {
    blocks.push(
      h(
        "div",
        {},
        h("strong", {}, "工作目录不存在："),
        h("code", {}, module.cwd),
        " —— 该模块的命令已禁用，请检查模块配置里的 cwd。"
      )
    );
  }
  if (!state.modules.length) {
    blocks.push(h("div", {}, "没有可用模块：请检查 modules/ 目录，或点右上角「重载配置」。"));
  }
  if (!blocks.length) {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }
  banner.hidden = false;
  banner.className = "banner" + (state.errors.length ? " danger" : "");
  banner.replaceChildren(...blocks);
}

function renderModuleNotes() {
  const box = $("#module-notes");
  const module = activeModule();
  const notes = module && Array.isArray(module.notes) ? module.notes : [];
  if (!notes.length) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.hidden = false;
  box.replaceChildren(h("ul", {}, ...notes.map((note) => h("li", {}, note))));
}

function renderHistory() {
  const box = $("#history-list");
  box.replaceChildren();
  if (!state.history.length) {
    box.append(h("p", { class: "empty" }, "还没有运行记录"));
    return;
  }
  for (const entry of state.history) {
    const dotClass =
      entry.status === "exited"
        ? "ok"
        : entry.status === "failed"
        ? "fail"
        : entry.status === "timed_out"
        ? "timeout"
        : entry.status === "cancelled"
        ? "cancel"
        : "running";
    box.append(
      h(
        "button",
        {
          type: "button",
          class: "history-item" + (state.run && state.run.runId === entry.runId ? " active" : ""),
          title: entry.display || "",
          on: { click: () => viewHistory(entry) },
        },
        h("span", { class: `history-dot ${dotClass}`, attrs: { "aria-hidden": "true" } }),
        h(
          "span",
          { class: "history-text" },
          h("span", { class: "history-name" }, entry.commandName),
          h(
            "span",
            { class: "history-meta" },
            `${timeShort(entry.at)} · 退出码 ${entry.code == null ? "—" : entry.code} · ${formatDuration(entry.durationMs)}`
          )
        )
      )
    );
  }
}

/* ================================================================== */
/* 渲染：命令列表                                                      */
/* ================================================================== */

function renderCommands() {
  const items = visibleItems();
  if (!items.some((item) => item.key === state.selectedKey)) {
    state.selectedKey = items.length ? items[0].key : null;
  }
  const list = $("#command-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(
      h(
        "p",
        { class: "empty" },
        state.query ? `没有匹配「${state.query}」的命令` : "当前筛选条件下没有命令"
      )
    );
  } else {
    for (const item of items) list.append(commandCard(item));
  }

  const module = activeModule();
  const total = state.modules.reduce((sum, m) => sum + m.commands.length, 0);
  $("#list-meta").textContent = state.query
    ? `在全部模块中找到 ${items.length} 条命令（共 ${total} 条）`
    : `${module ? module.name : "—"} · ${items.length} 条命令${state.riskFilter !== "all" ? "（已筛选）" : ""}`;

  renderModuleNotes();

  requestAnimationFrame(() => {
    const selected = list.querySelector(".cmd.selected");
    if (selected) selected.scrollIntoView({ block: "nearest" });
  });
}

function commandCard({ module, command, key }) {
  const preview = state.previews[key] || { display: "", error: "" };
  const values = state.params[key] || {};
  const selected = key === state.selectedKey;
  const risk = RISK[command.risk] || { label: command.risk, cls: "low" };

  const card = h("article", {
    class:
      `cmd risk-${command.risk}` +
      (selected ? " selected" : "") +
      (preview.error ? " has-error" : ""),
    tabIndex: 0,
    dataset: { key, commandId: command.id, moduleId: module.id },
    attrs: { role: "option", "aria-selected": selected ? "true" : "false" },
    on: {
      click: (event) => {
        if (event.target.closest("button, input, label")) return;
        selectKey(key);
      },
      focus: () => {
        if (state.selectedKey !== key) selectKey(key);
      },
    },
  });

  card.append(
    h(
      "div",
      { class: "cmd-head" },
      h("span", { class: `risk-badge risk-${command.risk}` }, risk.label),
      h("h3", { class: "cmd-name" }, command.name),
      command.needsConfirm ? h("span", { class: "confirm-chip" }, "需二次确认") : null,
      h("span", { class: "cmd-id" }, command.id)
    ),
    h("p", { class: "cmd-desc" }, command.description || "（无说明）"),
    h(
      "div",
      { class: "cmd-code" },
      h("code", { class: "cmd-text" }, preview.error ? "（参数不合法，见下方提示）" : preview.display || "…"),
      h(
        "button",
        {
          class: "ghost-btn copy-btn",
          type: "button",
          title: "复制这条命令（c）",
          on: {
            click: (event) => {
              event.stopPropagation();
              const current = state.previews[key] || {};
              if (current.error) {
                showToast("参数不合法，无法复制", "error");
                return;
              }
              copyText(current.display, "命令已复制");
            },
          },
        },
        "复制"
      )
    )
  );

  if (command.params.length) {
    card.append(
      h(
        "div",
        { class: "params" },
        ...command.params.map((param) =>
          h(
            "label",
            { class: "param", title: param.description || "" },
            h("span", { class: "param-label" }, param.label),
            h("input", {
              type: "text",
              class: "param-input",
              value: values[param.name] != null ? values[param.name] : param.default,
              spellcheck: false,
              attrs: { "aria-label": param.label, placeholder: param.default },
              dataset: { param: param.name },
              on: {
                input: (event) => {
                  if (!state.params[key]) state.params[key] = {};
                  state.params[key][param.name] = event.target.value;
                  schedulePreview(module, command);
                },
              },
            })
          )
        )
      )
    );
  }

  card.append(
    h("p", { class: "param-error" }, preview.error || ""),
    h(
      "div",
      { class: "cmd-actions" },
      h(
        "button",
        {
          class: "run-btn",
          type: "button",
          disabled: !!preview.error || !module.cwdExists,
          on: {
            click: (event) => {
              event.stopPropagation();
              selectKey(key);
              requestRun(module, command);
            },
          },
        },
        command.needsConfirm ? "执行（需确认）↵" : "执行 ↵"
      ),
      h("span", { class: "cmd-meta" }, `超时 ${command.timeoutSec}s`),
      command.params.length ? h("span", { class: "cmd-meta" }, "参数可修改") : null
    )
  );

  return card;
}

function selectKey(key, { focus = false } = {}) {
  if (!key) return;
  if (state.selectedKey === key) {
    if (focus) {
      const card = document.querySelector(`.cmd[data-key="${key}"]`);
      if (card) card.focus();
    }
    return;
  }
  state.selectedKey = key;
  renderCommands();
  if (focus) {
    const card = document.querySelector(`.cmd[data-key="${key}"]`);
    if (card) card.focus();
  }
}

function moveSelection(delta) {
  const items = visibleItems();
  if (!items.length) return;
  let index = items.findIndex((item) => item.key === state.selectedKey);
  index = index === -1 ? 0 : Math.max(0, Math.min(items.length - 1, index + delta));
  state.selectedKey = items[index].key;
  renderCommands();
  const card = document.querySelector(`.cmd[data-key="${state.selectedKey}"]`);
  if (card) card.focus();
}

/* ================================================================== */
/* 参数预览（服务端是唯一渲染真源）                                     */
/* ================================================================== */

function schedulePreview(module, command) {
  const key = keyOf(module, command);
  clearTimeout(state.renderTimers[key]);
  state.renderTimers[key] = setTimeout(() => {
    renderPreview(module, command);
  }, 140);
}

async function renderPreview(module, command, { silent = true } = {}) {
  const key = keyOf(module, command);
  try {
    const data = await api("/api/render", {
      method: "POST",
      body: {
        moduleId: module.id,
        commandId: command.id,
        params: state.params[key] || {},
      },
    });
    state.previews[key] = {
      display: data.display,
      argv: data.argv || [],
      usesShell: !!data.usesShell,
      error: "",
    };
  } catch (err) {
    state.previews[key] = {
      ...(state.previews[key] || {}),
      error: err.message || String(err),
    };
    if (!silent) showToast(`参数不合法：${err.message}`, "error");
  }
  applyPreviewToCard(key, module);
  return state.previews[key];
}

function applyPreviewToCard(key, module) {
  const card = document.querySelector(`.cmd[data-key="${key}"]`);
  if (!card) return;
  const preview = state.previews[key] || {};
  const codeElement = card.querySelector(".cmd-text");
  const errorElement = card.querySelector(".param-error");
  const runButton = card.querySelector(".run-btn");
  if (codeElement) {
    codeElement.textContent = preview.error ? "（参数不合法，见下方提示）" : preview.display || "…";
  }
  card.classList.toggle("has-error", !!preview.error);
  if (errorElement) errorElement.textContent = preview.error || "";
  if (runButton) runButton.disabled = !!preview.error || !module.cwdExists;
}

/* ================================================================== */
/* 执行                                                                */
/* ================================================================== */

async function requestRun(module, command) {
  if (state.busy) return;
  if (state.run && state.run.status === "running" && !state.run.replay) {
    showToast("已有命令在运行，先停止或等它结束", "warn");
    return;
  }
  if (!module.cwdExists) {
    showToast(`工作目录不存在：${module.cwd}`, "error");
    return;
  }
  const key = keyOf(module, command);
  if ((state.previews[key] || {}).error) {
    showToast("参数不合法，先修正参数", "error");
    return;
  }
  state.busy = true;
  try {
    const preview = await renderPreview(module, command, { silent: false });
    if (!preview || preview.error) return;
    if (command.needsConfirm) {
      openConfirm(module, command);
      return;
    }
    await startRun(module, command, { confirmed: false });
  } finally {
    state.busy = false;
  }
}

async function startRun(module, command, { confirmed = false } = {}) {
  const key = keyOf(module, command);
  const body = {
    moduleId: module.id,
    commandId: command.id,
    params: state.params[key] || {},
  };
  if (confirmed) body.confirm = true;
  let data;
  try {
    data = await api("/api/run", { method: "POST", body });
  } catch (err) {
    if (err.code === "confirm_required") {
      openConfirm(module, command);
      return;
    }
    showToast(`启动失败：${err.message}`, "error");
    return;
  }
  attachRun({
    runId: data.runId,
    display: data.display,
    module,
    command,
    replay: false,
  });
}

function attachRun({ runId, display, module, command, replay = false }) {
  if (state.run && state.run.es) {
    try {
      state.run.es.close();
    } catch (err) {
      /* 忽略 */
    }
  }
  resetOutput();
  const run = {
    runId,
    moduleId: module.id,
    commandId: command.id,
    moduleName: module.name,
    commandName: command.name,
    display: display || (state.previews[keyOf(module, command)] || {}).display || "",
    status: "running",
    code: null,
    durationMs: null,
    timedOut: false,
    cancelled: false,
    replay,
    es: null,
    outputText: "",
    startedWall: Date.now(),
    pid: null,
  };
  state.run = run;
  renderRunPanel();
  renderHistory();
  startElapsedTimer();
  openRunStream(run);
}

async function viewHistory(entry) {
  let payload;
  try {
    payload = await api(`/api/runs/${entry.runId}`);
  } catch (err) {
    showToast("运行记录已不存在（服务重启或记录被清理）", "warn");
    return;
  }
  if (state.run && state.run.es) {
    try {
      state.run.es.close();
    } catch (err) {
      /* 忽略 */
    }
  }
  resetOutput();
  const run = {
    ...entry,
    status: payload.run.status || entry.status,
    code: payload.run.exitCode != null ? payload.run.exitCode : entry.code,
    durationMs: payload.run.durationMs != null ? payload.run.durationMs : entry.durationMs,
    timedOut: !!payload.run.timedOut,
    cancelled: !!payload.run.cancelled,
    replay: true,
    es: null,
    outputText: "",
    startedWall: Date.now(),
    pid: null,
  };
  state.run = run;
  renderRunPanel();
  renderHistory();
  openRunStream(run);
}

function openRunStream(run) {
  const source = new EventSource(`/api/runs/${run.runId}/events?from=0`);
  run.es = source;

  source.addEventListener("start", (event) => {
    const data = safeJson(event.data);
    if (!data || state.run !== run) return;
    if (typeof data.display === "string" && data.display) {
      run.display = data.display;
      $("#run-cmd-text").textContent = data.display;
    }
  });

  source.addEventListener("pid", (event) => {
    const data = safeJson(event.data);
    if (!data || state.run !== run) return;
    run.pid = data.pid;
    renderRunPanel();
  });

  source.addEventListener("stdout", (event) => {
    const data = safeJson(event.data);
    if (!data || state.run !== run) return;
    appendOutput("stdout", String(data.text == null ? "" : data.text));
  });

  source.addEventListener("stderr", (event) => {
    const data = safeJson(event.data);
    if (!data || state.run !== run) return;
    appendOutput("stderr", String(data.text == null ? "" : data.text));
  });

  source.addEventListener("notice", (event) => {
    const data = safeJson(event.data);
    if (!data || state.run !== run) return;
    appendOutput("notice", `[提示] ${data.message}\n`);
  });

  source.addEventListener("exit", (event) => {
    const data = safeJson(event.data) || {};
    if (state.run === run) finishRun(run, data);
    source.close();
  });

  source.addEventListener("end", () => source.close());

  source.addEventListener("error", () => {
    run.errorCount = (run.errorCount || 0) + 1;
    if (run.errorCount > 3) {
      source.close();
      showToast("事件流连接失败，可刷新页面重试", "error");
      return;
    }
    if (state.run === run && run.status === "running") {
      $("#run-hint").textContent = "连接中断，正在自动重连…";
    }
  });
}

function finishRun(run, data) {
  run.status = data.status || (data.timedOut ? "timed_out" : data.cancelled ? "cancelled" : "exited");
  run.code = data.code == null ? null : data.code;
  run.durationMs = data.durationMs == null ? null : data.durationMs;
  run.timedOut = !!data.timedOut;
  run.cancelled = !!data.cancelled;
  stopElapsedTimer();

  const parts = [
    `\n[进程结束] 退出码 ${run.code == null ? "无" : run.code}`,
    `耗时 ${formatDuration(run.durationMs)}`,
  ];
  if (run.timedOut) parts.push("已超时终止");
  if (run.cancelled) parts.push("已手动停止");
  if (data.error) parts.push(`错误：${data.error}`);
  appendOutput("exit", parts.join(" · ") + "\n");

  if (state.run === run) {
    renderRunPanel();
    renderHistory();
  }
  pushHistory(run);
}

function pushHistory(run) {
  if (!run || run.replay) return;
  if (state.history.some((entry) => entry.runId === run.runId)) return;
  state.history.unshift({
    runId: run.runId,
    moduleId: run.moduleId,
    commandId: run.commandId,
    moduleName: run.moduleName,
    commandName: run.commandName,
    display: run.display,
    status: displayStatusOf(run),
    code: run.code,
    durationMs: run.durationMs,
    at: Date.now(),
  });
  state.history = state.history.slice(0, 20);
  renderHistory();
}

function displayStatusOf(run) {
  if (run.status === "exited") return run.code === 0 ? "exited" : "failed";
  return run.status;
}

/* ================================================================== */
/* 执行面板                                                            */
/* ================================================================== */

function resetOutput() {
  const output = $("#run-output");
  output.replaceChildren();
  output.scrollTop = 0;
}

function appendOutput(channel, text) {
  if (!text) return;
  const output = $("#run-output");
  const span = h("span", { class: `out-${channel}` });
  span.textContent = text;
  output.append(span);
  if (state.run) state.run.outputText += text;
  while (output.childNodes.length > 4000) {
    output.removeChild(output.firstChild);
  }
  if ($("#autoscroll").checked) output.scrollTop = output.scrollHeight;
}

function renderRunPanel() {
  const run = state.run;
  const statusElement = $("#run-status");
  const exitElement = $("#run-exit");
  const durationElement = $("#run-duration");
  const pidElement = $("#run-pid");
  const titleElement = $("#run-title");
  const eyebrow = $("#run-eyebrow");
  const commandText = $("#run-cmd-text");
  const cancelButton = $("#run-cancel");
  const copyOutputButton = $("#run-copy-out");
  const rerunButton = $("#run-rerun");
  const hint = $("#run-hint");

  if (!run) {
    titleElement.textContent = "尚未执行";
    eyebrow.textContent = "执行输出";
    statusElement.className = "pill idle";
    statusElement.textContent = "空闲";
    exitElement.textContent = "退出码 —";
    durationElement.textContent = "耗时 —";
    pidElement.textContent = "";
    commandText.textContent = "选择左侧命令并执行，输出会实时出现在这里";
    $("#run-cmd-copy").disabled = true;
    cancelButton.disabled = true;
    copyOutputButton.disabled = true;
    rerunButton.disabled = true;
    hint.textContent = "";
    return;
  }

  titleElement.textContent = `${run.moduleName} / ${run.commandName}`;
  eyebrow.textContent = run.replay ? "历史回放" : "执行输出";
  const status = STATUS[displayStatusOf(run)] || STATUS.idle;
  statusElement.className = `pill ${status.cls}`;
  statusElement.textContent = status.label;
  exitElement.textContent = `退出码 ${run.code == null ? "—" : run.code}`;
  durationElement.textContent = `耗时 ${
    run.status === "running" || run.status === "starting"
      ? formatDuration(Date.now() - run.startedWall)
      : formatDuration(run.durationMs)
  }`;
  pidElement.textContent = run.pid ? `pid ${run.pid}` : "";
  commandText.textContent = run.display || "…";
  $("#run-cmd-copy").disabled = false;
  cancelButton.disabled = !(run.status === "running" || run.status === "starting") || run.replay;
  copyOutputButton.disabled = !run.outputText;
  rerunButton.disabled = false;
  hint.textContent = run.status === "running" && !run.replay ? "运行中…（可停止）" : "";
}

function startElapsedTimer() {
  stopElapsedTimer();
  elapsedTimer = setInterval(() => {
    if (!state.run || state.run.status !== "running") return;
    $("#run-duration").textContent = `耗时 ${formatDuration(Date.now() - state.run.startedWall)}`;
  }, 100);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

/* ================================================================== */
/* 危险确认 / 帮助 / 搜索                                               */
/* ================================================================== */

function openConfirm(module, command) {
  const key = keyOf(module, command);
  const preview = state.previews[key] || {};
  state.confirmCtx = { module, command };
  state.confirmOpenedAt = performance.now();
  const modal = $("#modal");
  $("#modal-title").textContent = "确认执行危险命令";
  $("#modal-body").replaceChildren(
    h(
      "div",
      { class: "confirm-grid" },
      h("span", { class: `risk-badge risk-${command.risk}` }, `危险级：${(RISK[command.risk] || {}).label || command.risk}`),
      h("span", { class: "confirm-name" }, `${module.name} / ${command.name}`)
    ),
    h("p", { class: "confirm-hint" }, command.confirmHint || "该命令可能改变系统状态，请确认命令内容无误。"),
    h("pre", { class: "confirm-cmd" }, preview.display || "…"),
    h("p", { class: "modal-note" }, "点击「我确认执行」或按 Enter 才会真正运行；Esc 取消。")
  );
  modal.hidden = false;
  $("#modal-confirm").disabled = false;
  $("#modal-confirm").focus();
}

function closeConfirm() {
  state.confirmCtx = null;
  $("#modal").hidden = true;
}

function toggleHelp(open) {
  const modal = $("#help");
  const shouldOpen = open == null ? modal.hidden : open;
  modal.hidden = !shouldOpen;
  if (shouldOpen) $("#help-close").focus();
}

function clearSearch() {
  const search = $("#search");
  search.value = "";
  state.query = "";
}

/* ================================================================== */
/* 键盘                                                                */
/* ================================================================== */

function onGlobalKeydown(event) {
  if (event.isComposing) return;

  const modalOpen = !$("#modal").hidden;
  const helpOpen = !$("#help").hidden;
  if (modalOpen || helpOpen) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (modalOpen) closeConfirm();
      if (helpOpen) toggleHelp(false);
    }
    return;
  }

  const inField = event.target instanceof Element && event.target.matches("input, textarea, select");
  const onButton = event.target instanceof Element && !!event.target.closest("button");

  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#search").focus();
    $("#search").select();
    return;
  }

  if (event.key === "Escape") {
    if (inField) {
      event.target.blur();
      return;
    }
    if (state.query) {
      clearSearch();
      renderCommands();
    }
    return;
  }

  if (event.key === "?" || (event.shiftKey && event.key === "/")) {
    event.preventDefault();
    toggleHelp();
    return;
  }

  if (inField) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      const item = currentItem();
      if (item) {
        event.preventDefault();
        requestRun(item.module, item.command);
      }
    }
    return;
  }

  if (onButton && (event.key === "Enter" || event.key === " ")) {
    return;
  }

  if (event.key === "/") {
    event.preventDefault();
    $("#search").focus();
    $("#search").select();
    return;
  }
  if (event.key === "ArrowDown" || event.key === "j") {
    event.preventDefault();
    moveSelection(1);
    return;
  }
  if (event.key === "ArrowUp" || event.key === "k") {
    event.preventDefault();
    moveSelection(-1);
    return;
  }
  if (event.key === "Home") {
    const items = visibleItems();
    if (items.length) {
      event.preventDefault();
      selectKey(items[0].key, { focus: true });
    }
    return;
  }
  if (event.key === "End") {
    const items = visibleItems();
    if (items.length) {
      event.preventDefault();
      selectKey(items[items.length - 1].key, { focus: true });
    }
    return;
  }
  if (event.key === "Enter") {
    const item = currentItem();
    if (item) {
      event.preventDefault();
      requestRun(item.module, item.command);
    }
    return;
  }
  if (event.key === "c") {
    event.preventDefault();
    copySelectedCommand();
    return;
  }
  if (event.key === "r") {
    event.preventDefault();
    reloadConfigs();
  }
}

function copySelectedCommand() {
  const item = currentItem();
  if (!item) return;
  const preview = state.previews[item.key] || {};
  if (preview.error) {
    showToast("参数不合法，无法复制", "error");
    return;
  }
  copyText(preview.display, "命令已复制");
}

/* ================================================================== */
/* 事件绑定与初始化                                                     */
/* ================================================================== */

function bindEvents() {
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.selectedKey = null;
      renderCommands();
    }, 80);
  });

  $("#search").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.query) {
        clearSearch();
        renderCommands();
      }
      event.target.blur();
      event.stopPropagation();
    }
  });

  $("#reload-btn").addEventListener("click", reloadConfigs);
  $("#help-btn").addEventListener("click", () => toggleHelp(true));
  $("#health-pill").addEventListener("click", async () => {
    await pollHealth();
    showToast("状态已刷新", "ok", 1400);
  });

  $("#modal-cancel").addEventListener("click", closeConfirm);
  $("#modal-confirm").addEventListener("click", () => {
    if (performance.now() - state.confirmOpenedAt < 250) return;
    const context = state.confirmCtx;
    if (!context) return;
    closeConfirm();
    startRun(context.module, context.command, { confirmed: true });
  });
  $$("[data-close]").forEach((element) => {
    element.addEventListener("click", () => {
      closeConfirm();
      toggleHelp(false);
    });
  });
  $("#help-close").addEventListener("click", () => toggleHelp(false));

  $("#run-cmd-copy").addEventListener("click", () => {
    copyText(state.run && state.run.display, "命令已复制");
  });
  $("#run-copy-out").addEventListener("click", () => {
    copyText(state.run && state.run.outputText, "输出已复制");
  });
  $("#run-cancel").addEventListener("click", async () => {
    if (!state.run || state.run.replay) return;
    try {
      await api(`/api/runs/${state.run.runId}/cancel`, { method: "POST", body: {} });
      showToast("已请求停止，等待进程退出…", "warn", 2000);
    } catch (err) {
      showToast(`停止失败：${err.message}`, "error");
    }
  });
  $("#run-rerun").addEventListener("click", () => {
    const run = state.run;
    if (!run) return;
    const module = state.modules.find((m) => m.id === run.moduleId);
    const command = module && module.commands.find((c) => c.id === run.commandId);
    if (!module || !command) {
      showToast("找不到原命令，配置可能已变化", "error");
      return;
    }
    state.activeModuleId = module.id;
    renderAll();
    requestRun(module, command);
  });

  document.addEventListener("keydown", onGlobalKeydown);
  window.addEventListener("beforeunload", () => {
    if (state.run && state.run.es) state.run.es.close();
  });
}

function renderAll() {
  renderSidebar();
  renderRiskFilters();
  renderCommands();
  renderBanner();
  renderRunPanel();
  renderHistory();
}

(async function init() {
  bindEvents();
  renderRiskFilters();
  renderHistory();
  renderRunPanel();
  await pollHealth();
  try {
    await loadModules();
  } catch (err) {
    showToast(`加载模块失败：${err.message}`, "error");
    $("#banner").hidden = false;
    $("#banner").className = "banner danger";
    $("#banner").replaceChildren(h("div", {}, `加载模块失败：${err.message}`));
  }
  setInterval(pollHealth, 15000);
})();
