/* SimLab Studio: real sampled simulation, durable versions, and validated LLM edits. */
"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/studio";
const DAY = 86400;
const SVG = "http://www.w3.org/2000/svg";
const SESSION_KEY = "simlab.studio.session";
const clone = (value) => JSON.parse(JSON.stringify(value));
const state = {
  bootstrap: null, session: null, ai: null, draft: null, breaks: [], dirty: false,
  busy: false, adjusting: false, runs: new Map(), watchers: new Map(),
  frames: [], preview: null, frameIndex: 0, playing: false, animation: 0,
  lastTick: 0, elapsed: 0, pendingMessages: [], generation: 0,
  aiProfiles: null, aiEditorId: null, aiDrafts: new Map(), aiSaving: false,
  requestAI: null,
};
const labels = {
  cycle_time_seconds: "加工时间", availability: "可用率", mttr_seconds: "平均维修时间",
  idle_power_kw: "空闲功率", processing_power_kw: "加工功率", capacity: "容量",
  delay_seconds: "转运时间", until_seconds: "仿真时长", warmup_seconds: "预热时长",
  replications: "重复次数", base_seed: "随机种子", confidence_level: "置信水平",
  raw_buffer_capacity: "原料缓冲容量", name: "名称", mode: "约束模式",
  start_second: "停产开始", end_second: "停产结束",
};
const machineStates = {
  processing: {label: "加工中", color: "#397e64", fill: "#e9f3e8"},
  starved: {label: "等待来料", color: "#94a487", fill: "#f1f4e9"},
  blocked: {label: "出料堵塞", color: "#bb8b3e", fill: "#faf0da"},
  failed: {label: "故障维修", color: "#b4685e", fill: "#f8e9e2"},
  off_shift: {label: "计划停产", color: "#8393a0", fill: "#ecf0f2"},
  idle: {label: "待机", color: "#94a487", fill: "#f1f4e9"},
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function svgNode(tag, attrs = {}, text) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function svgText(parent, x, y, text, attrs = {}) {
  parent.append(svgNode("text", {x, y, fill: "#78916a", "font-size": 10,
    "font-family": '"Segoe UI","Microsoft YaHei",sans-serif', ...attrs}, text));
}
function text(id, value) { $(id).textContent = String(value); }
function showError(id, error) {
  const target = $(id);
  target.textContent = error ? String(error.message || error) : "";
  target.hidden = !error;
}
let toastTimeout;
function toast(message) {
  text("toast", message); $("toast").hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => { $("toast").hidden = true; }, 4200);
}
function format(value, decimals = 2) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? "—" : Number(value).toLocaleString("zh-CN", {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}
function durationLabel(seconds) {
  const days = Number(seconds) / DAY;
  if (days > 0 && days < 0.000001) return `${Number(seconds).toPrecision(3)} 秒`;
  return `${days.toLocaleString("zh-CN", {maximumFractionDigits: 6})} 天`;
}
function timeLabel(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const day = Math.floor(total / DAY) + 1;
  const hours = Math.floor((total % DAY) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return `第 ${day} 天 · ${[hours, minutes, secs].map((value) => String(value).padStart(2, "0")).join(":")}`;
}
function activeVersion() {
  return state.session?.versions.find((version) => version.id === state.session.active_version_id);
}
function versionName(version) {
  if (!version) return "未载入方案";
  const index = state.session.versions.findIndex((item) => item.id === version.id);
  return `V${index} · ${version.label}`;
}
function sessionPath(suffix = "") { return `/sessions/${state.session.id}${suffix}`; }
async function api(path, options = {}) {
  const response = await fetch(API + path, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    let detail = typeof data.detail === "string" ? data.detail : "请求未完成，请稍后重试。";
    const errors = data.errors || (Array.isArray(data.detail) ? data.detail : []);
    if (errors.length) detail += "\n" + errors.map((item) => `${item.path || (item.loc || []).join(".")}：${item.message || item.msg}`).join("\n");
    const error = new Error(detail); error.status = response.status; throw error;
  }
  return data;
}
function action(fn, errorId = "global-error") {
  return async (event) => {
    event?.preventDefault(); showError(errorId, null);
    try { await fn(event); } catch (error) { showError(errorId, error); }
  };
}
function bind(id, event, fn, errorId) { $(id).addEventListener(event, action(fn, errorId)); }
function persistSession() {
  try { localStorage.setItem(SESSION_KEY, state.session.id); } catch { /* Browser storage may be disabled. */ }
}
function hasPending(kind) {
  return state.session?.runs.some((run) => run.version_id === state.session.active_version_id &&
    run.kind === kind && ["queued", "running"].includes(run.status));
}
function updateControls() {
  const unavailable = !state.session || state.busy || state.adjusting;
  $("apply-model").disabled = unavailable;
  $("run-preview").disabled = unavailable || hasPending("preview");
  $("run-study").disabled = unavailable || hasPending("study");
  $("export-session").disabled = !state.session || state.adjusting;
  $("new-session").disabled = unavailable;
  $("import-config").disabled = unavailable;
  $("edit-breaks").disabled = unavailable;
  if ($("open-experiments")) $("open-experiments").disabled = unavailable;
  document.querySelectorAll("#model-body input, #model-body select").forEach((input) => {
    const paperCapacity = input.id.startsWith("buffer-") && input.id.endsWith("-capacity") && $("model-mode").value === "paper";
    input.disabled = unavailable || paperCapacity;
  });
  $("model-body").setAttribute("aria-busy", String(state.busy || state.adjusting));
  $("send-prompt").disabled = unavailable || !$("prompt").value.trim();
  $("prompt").disabled = state.adjusting;
  $("adjust-progress").hidden = !state.adjusting;
  for (const button of document.querySelectorAll(".version-restore")) button.disabled = unavailable;
  text("apply-model", state.dirty ? "应用修改并预览 →" : "保存模型并预览 →");
  text("save-status", state.dirty ? "有未应用的模型修改" : "模型与结果保存在本机");
}
function setDirty() { state.dirty = true; updateControls(); }
function renderAI() {
  const ai = state.ai || {};
  text("ai-status", ai.available ? "已配置" : "待配置");
  $("ai-status").classList.toggle("connected", Boolean(ai.available));
  $("ai-status").title = ai.available ? `${ai.model} · 配置已保存，调用时验证连接` : "请配置模型 API";
  const profiles = state.aiProfiles?.profiles || [];
  const select = $("ai-profile-select"); select.replaceChildren();
  for (const profile of profiles) {
    const option = node("option", "", `${profile.name} · ${profile.model}${profile.available ? "" : "（待配置密钥）"}`);
    option.value = profile.id; select.append(option);
  }
  if (!profiles.length) { const option = node("option", "", "正在载入模型配置…"); select.append(option); }
  select.value = state.aiProfiles?.active_profile_id || "";
  select.disabled = state.aiSaving || !profiles.length;
  $("ai-settings").disabled = state.aiSaving;
  $("manage-ai-profiles").disabled = state.aiSaving;
  select.title = ai.base_url || "https://api.openai.com/v1";
  text("ai-profile-caption", state.requestAI
    ? `本轮：${state.requestAI.model}；切换将在下一条提示词生效。`
    : "可随时切换，下一条提示词使用所选模型。");
  text("ai-hint", ai.available ? `${ai.model} · 修改通过校验后自动预览` : "在“模型接入”中填写该服务的密钥；手动仿真可直接使用。");
  updateControls();
}

function acceptAIProfiles(catalog) {
  state.aiProfiles = catalog;
  const active = catalog.profiles.find((profile) => profile.id === catalog.active_profile_id);
  state.ai = active ? {...active, profile_id: active.id, profile_name: active.name} : null;
  renderAI();
}
function aiSourceLabel(config) {
  if (!config) return "";
  return [config.profile_name || config.name, config.model].filter(Boolean).join(" · ");
}

function makeInput(label, id, value, constraints = {}, unit) {
  const wrapper = node("label", "field", label);
  const input = node("input");
  input.type = "number"; input.step = "any"; input.id = id;
  input.value = String(value); input.required = true;
  Object.assign(input, constraints);
  if (unit) wrapper.append(node("span", "", unit));
  wrapper.append(input);
  input.addEventListener("input", setDirty);
  return wrapper;
}
function renderEditor(config, mode) {
  state.draft = clone(config); state.breaks = clone(config.breaks || []); state.dirty = false;
  $("model-name").value = config.name;
  $("model-mode").value = mode;
  $("until-days").value = String(config.until_seconds / DAY);
  $("warmup-days").value = String(config.warmup_seconds / DAY);
  $("replications").value = String(config.replications);
  $("replications").max = String(state.bootstrap?.limits?.replications || 50);
  $("until-days").max = String(state.bootstrap?.limits?.days || 90);
  $("base-seed").value = String(config.base_seed);
  text("machine-count", `${config.machines.length} 台`);
  const machines = $("machine-fields"); machines.replaceChildren();
  config.machines.forEach((machine, index) => {
    const details = node("details", "machine-details"); details.open = index === 0;
    const summary = node("summary");
    summary.append(node("span", "machine-number", String(index + 1).padStart(2, "0")),
      node("span", "", machine.name), node("span", "machine-ct", `${format(machine.cycle_time_seconds, 0)} s`));
    const fields = node("div", "machine-inputs field-grid");
    fields.append(makeInput("加工时间", `machine-${index}-cycle_time_seconds`, machine.cycle_time_seconds, {min: "1"}, "s"),
      makeInput("可用率", `machine-${index}-availability`, machine.availability * 100, {min: "0.001", max: "100"}, "%"),
      makeInput("平均维修时间", `machine-${index}-mttr_seconds`, machine.mttr_seconds, {min: "0"}, "s"),
      makeInput("空闲功率", `machine-${index}-idle_power_kw`, machine.idle_power_kw, {min: "0"}, "kW"),
      makeInput("加工功率", `machine-${index}-processing_power_kw`, machine.processing_power_kw, {min: "0"}, "kW"));
    details.append(summary, fields); machines.append(details);
  });
  const buffers = $("buffer-fields"); buffers.replaceChildren();
  config.buffers.forEach((buffer, index) => {
    const row = node("div", "buffer-row");
    const name = node("span", "", `B${index + 1} · ${buffer.name}`); name.title = buffer.name;
    row.append(name,
      makeInput("容量", `buffer-${index}-capacity`, buffer.capacity, {min: "1", max: "10000", step: "1"}),
      makeInput("转运 / s", `buffer-${index}-delay_seconds`, buffer.delay_seconds, {min: "0"}));
    buffers.append(row);
  });
  updateMode(); renderBreakSummary(); showError("model-error", null); updateControls();
}
function updateMode() {
  const paper = $("model-mode").value === "paper";
  document.querySelectorAll('[id^="buffer-"][id$="-capacity"]').forEach((input) => { input.disabled = paper; });
  text("mode-note", paper ? "保留案例一设备顺序与容量为 5 的缓冲区。" : "可调整缓冲容量；可导入最多 12 台设备的串行模型。");
}
function readDraft() {
  const config = clone(state.draft);
  const value = (id) => {
    const input = $(id);
    const number = Number(input.value);
    if (!input.value.trim() || !Number.isFinite(number)) throw new Error("请完整填写数值参数。");
    return number;
  };
  config.name = $("model-name").value.trim();
  if (!config.name) throw new Error("请输入模型名称。");
  config.until_seconds = value("until-days") * DAY;
  config.warmup_seconds = value("warmup-days") * DAY;
  config.replications = value("replications"); config.base_seed = value("base-seed");
  config.breaks = clone(state.breaks);
  config.machines.forEach((machine, index) => {
    for (const key of ["cycle_time_seconds", "availability", "mttr_seconds", "idle_power_kw", "processing_power_kw"]) {
      machine[key] = value(`machine-${index}-${key}`) / (key === "availability" ? 100 : 1);
    }
  });
  config.buffers.forEach((buffer, index) => {
    buffer.capacity = value(`buffer-${index}-capacity`);
    buffer.delay_seconds = value(`buffer-${index}-delay_seconds`);
  });
  if (config.warmup_seconds >= config.until_seconds) throw new Error("预热时长必须小于仿真时长。");
  return config;
}
async function applyModel() {
  if (!$("model-form").reportValidity()) return;
  const config = readDraft(); const mode = $("model-mode").value;
  state.busy = true; pause(); updateControls();
  try {
    if (JSON.stringify(config) !== JSON.stringify(activeVersion().config) || mode !== activeVersion().mode) {
      const session = await api(sessionPath("/versions"), {method: "POST", body: {
        config, mode, label: "手动调整", expected_version_id: state.session.active_version_id,
      }});
      acceptSession(session);
      toast("修改已保存为新版本，正在生成预览。");
    } else state.dirty = false;
    await startRun("preview");
  } finally { state.busy = false; updateControls(); }
}
function acceptSession(session) {
  const previousHead = state.session?.active_version_id;
  state.session = session; persistSession();
  for (const run of session.runs) {
    const cached = state.runs.get(run.id);
    state.runs.set(run.id, {...cached, ...run, result: cached?.result || run.result});
  }
  const version = activeVersion();
  renderEditor(version.config, version.mode);
  text("version-badge", `V${session.versions.length - 1} · ${version.mode === "paper" ? "论文模式" : "自定义模式"}`);
  text("active-label", versionName(version));
  if (previousHead !== session.active_version_id) resetReplay();
  renderMessages(); renderVersions(); updateControls();
}
async function createSession() {
  state.busy = true; updateControls(); pause();
  try {
    const session = await api("/sessions", {method: "POST", body: {config: state.bootstrap.template, mode: "paper"}});
    state.generation += 1; state.pendingMessages = []; state.runs.clear();
    acceptSession(session);
  } finally { state.busy = false; updateControls(); }
}

function drawLine(frame) {
  const config = activeVersion()?.config || state.bootstrap?.template;
  if (!config) return;
  const svg = $("production-line"); svg.replaceChildren();
  const count = config.machines.length;
  const width = Math.max(440, 120 + count * 165);
  svg.setAttribute("viewBox", `0 0 ${width} 238`);
  svg.style.minWidth = `${Math.max(520, Math.min(1700, count * 125))}px`;
  const centerY = 111; const machineWidth = 110;
  const firstX = 80; const finalX = firstX + (count - 1) * 165 + machineWidth;
  const defs = svgNode("defs");
  const marker = svgNode("marker", {id: "line-arrow", markerWidth: 5, markerHeight: 5, refX: 4, refY: 2.5, orient: "auto"});
  marker.append(svgNode("path", {d: "M0,0 L5,2.5 L0,5", fill: "#a4b997"})); defs.append(marker); svg.append(defs);
  svg.append(svgNode("line", {x1: 31, x2: width - 30, y1: centerY, y2: centerY, stroke: "#d5dfcd", "stroke-width": 11, "stroke-linecap": "round"}));
  svg.append(svgNode("line", {x1: 31, x2: width - 30, y1: centerY, y2: centerY, stroke: "#fafcf7", "stroke-width": 1, "stroke-dasharray": "3 5"}));
  svg.append(svgNode("rect", {x: 14, y: 94, width: 34, height: 34, rx: 7, fill: "#f0f5e8", stroke: "#cbdabd"}));
  svgText(svg, 31, 116, "∞", {"text-anchor": "middle", "font-size": 19, fill: "#87a573"});
  svgText(svg, 31, 151, "原料", {"text-anchor": "middle", "font-size": 9});
  config.machines.forEach((spec, index) => {
    const machine = frame?.machines[index] || {state: "idle", progress: 0, holding_part: false, failures: 0};
    const appearance = machineStates[machine.state] || machineStates.idle;
    const x = firstX + index * 165;
    const group = svgNode("g", {"data-machine": index, "data-state": machine.state});
    group.append(svgNode("title", {}, `${spec.name}：${appearance.label}；加工进度 ${Math.round(machine.progress * 100)}%；故障 ${machine.failures || 0} 次`));
    svgText(group, x, 44, `M${index + 1}`, {"font-size": 9, fill: "#a0b18f"});
    svgText(group, x + machineWidth, 44, `${format(spec.cycle_time_seconds, 0)} s`, {"text-anchor": "end", "font-size": 9, fill: "#93a784"});
    group.append(svgNode("rect", {x, y: 55, width: machineWidth, height: 105, rx: 10,
      fill: appearance.fill, stroke: appearance.color, "stroke-width": 1.15, "stroke-opacity": .6}));
    group.append(svgNode("rect", {x: x + 13, y: 72, width: 67, height: 57, rx: 4, fill: "#ffffffaa", stroke: appearance.color, "stroke-opacity": .25}));
    group.append(svgNode("rect", {x: x + 21, y: 80, width: 48, height: 6, rx: 2, fill: appearance.color, opacity: .2}));
    group.append(svgNode("path", {d: `M${x + 45},86 v12 l-5,7 h10 l-5,-7`, stroke: appearance.color, "stroke-width": 1.5, fill: appearance.color, "fill-opacity": .22}));
    group.append(svgNode("rect", {x: x + 19, y: 117, width: 55, height: 4, rx: 1, fill: appearance.color, opacity: .22}));
    for (let light = 0; light < 3; light += 1) group.append(svgNode("circle", {
      cx: x + 94, cy: 80 + light * 10, r: 2.5, fill: appearance.color, opacity: light === 0 ? .9 : .17,
    }));
    if (machine.holding_part) {
      const progress = Math.max(0, Math.min(1, machine.progress || 0));
      group.append(svgNode("rect", {x: x + 21 + progress * 37, y: 107, width: 12, height: 9, rx: 2,
        fill: appearance.color, "data-part": "holding"}));
    }
    svgText(group, x + 13, 147, `${Math.round((machine.progress || 0) * 100)}%`, {"font-size": 9, fill: appearance.color});
    group.append(svgNode("rect", {x: x + 41, y: 141, width: 54, height: 4, rx: 2, fill: appearance.color, opacity: .14}));
    group.append(svgNode("rect", {x: x + 41, y: 141, width: 54 * Math.max(0, Math.min(1, machine.progress || 0)), height: 4, rx: 2, fill: appearance.color}));
    const displayName = spec.name.length > 15 ? `${spec.name.slice(0, 13)}…` : spec.name;
    svgText(group, x + 55, 180, displayName, {"text-anchor": "middle", "font-size": 11, fill: "#466737", "font-weight": 600});
    group.append(svgNode("circle", {cx: x + 26, cy: 197, r: 2.7, fill: appearance.color}));
    svgText(group, x + 34, 200, appearance.label, {"font-size": 9, fill: appearance.color});
    svg.append(group);
    if (index < config.buffers.length) {
      const bufferSpec = config.buffers[index];
      const buffer = frame?.buffers[index] || {level: 0, ready: 0, capacity: bufferSpec.capacity};
      const bx = x + 129;
      svg.append(svgNode("line", {x1: x + 112, x2: bx - 5, y1: centerY, y2: centerY, stroke: "#a4b997", "stroke-width": 1, "marker-end": "url(#line-arrow)"}));
      const bufferGroup = svgNode("g", {"data-buffer": index});
      bufferGroup.append(svgNode("title", {}, `${bufferSpec.name}：${buffer.level} / ${buffer.capacity} 件，其中可取 ${buffer.ready} 件`));
      bufferGroup.append(svgNode("rect", {x: bx - 1, y: 86, width: 24, height: 45, rx: 4, fill: "#f4f8ee", stroke: "#c6d6ba"}));
      const filled = Math.ceil(Math.min(1, buffer.level / buffer.capacity) * 5);
      for (let slot = 0; slot < 5; slot += 1) bufferGroup.append(svgNode("rect", {
        x: bx + 4, y: 122 - slot * 7, width: 14, height: 4, rx: 1,
        fill: slot < filled ? "#91ad7a" : "#e0e8d8",
      }));
      svgText(bufferGroup, bx + 11, 78, `B${index + 1}`, {"text-anchor": "middle", "font-size": 8, fill: "#9bb088"});
      svgText(bufferGroup, bx + 11, 148, `${buffer.level}/${buffer.capacity}`, {"text-anchor": "middle", "font-size": 9});
      svg.append(bufferGroup);
    }
  });
  const sinkX = width - 39;
  svg.append(svgNode("line", {x1: finalX + 4, x2: sinkX - 22, y1: centerY, y2: centerY, stroke: "#a4b997", "stroke-width": 1, "marker-end": "url(#line-arrow)"}));
  svg.append(svgNode("rect", {x: sinkX - 17, y: 94, width: 34, height: 34, rx: 7, fill: "#e9f2e2", stroke: "#c5d9b8"}));
  svgText(svg, sinkX, 115, "✓", {"text-anchor": "middle", "font-size": 17, fill: "#73a059"});
  svgText(svg, sinkX, 151, `${format(frame?.completed_total || 0, 0)} 件`, {"text-anchor": "middle", "font-size": 9});
  svgText(svg, sinkX, 167, "累计完工", {"text-anchor": "middle", "font-size": 8, fill: "#a5b796"});
  svg.setAttribute("aria-label", frame ? `${timeLabel(frame.time_seconds)}，${frame.machines.map((machine) => `${machine.name} ${machineStates[machine.state]?.label || machine.state}`).join("；")}；累计完工 ${frame.completed_total} 件` : `${count} 台设备串联生产线，等待运行`);
}
function drawSparkline(id, metric, color) {
  const canvas = $(id); const rect = canvas.getBoundingClientRect();
  const width = Math.max(50, rect.width); const height = Math.max(20, rect.height);
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const end = Math.min(state.frameIndex, state.frames.length - 1);
  const data = [];
  const stride = Math.max(1, Math.floor(end / 220));
  for (let index = 0; index <= end; index += stride) {
    const frame = state.frames[index];
    if (frame && frame.time_seconds > (state.preview?.warmup_seconds || 0) && Number.isFinite(frame.metrics?.[metric])) data.push({index, value: frame.metrics[metric]});
  }
  ctx.strokeStyle = "#edf2e7"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, height - 2); ctx.lineTo(width, height - 2); ctx.stroke();
  if (data.length < 2) return;
  const min = Math.min(...data.map((item) => item.value));
  const max = Math.max(...data.map((item) => item.value));
  const span = max - min || Math.abs(max) * .1 || 1;
  const points = data.map((item, index) => ({x: index * width / (data.length - 1), y: height - 4 - (item.value - min) / span * (height - 9)}));
  const gradient = ctx.createLinearGradient(0, 0, 0, height); gradient.addColorStop(0, `${color}20`); gradient.addColorStop(1, `${color}00`);
  ctx.beginPath(); ctx.moveTo(points[0].x, height); points.forEach((point) => ctx.lineTo(point.x, point.y)); ctx.lineTo(width, height); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
  ctx.beginPath(); points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y)); ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();
}
function renderFrame() {
  const frame = state.frames[state.frameIndex];
  drawLine(frame);
  text("line-clock", timeLabel(frame?.time_seconds || 0));
  const warming = frame && frame.time_seconds <= (state.preview?.warmup_seconds || 0);
  const metrics = frame?.metrics;
  text("metric-throughput", warming ? "—" : format(metrics?.throughput_per_hour));
  text("metric-wip", warming ? "—" : format(metrics?.average_wip ?? metrics?.avg_wip));
  text("metric-energy", warming ? "—" : format(metrics?.specific_energy_kwh_per_part));
  text("metric-window", warming ? "预热中 · 预热结束后开始统计" : "单次预览 · 预热后累计统计");
  $("seek").value = String(state.frameIndex);
  text("replay-percent", `${state.frames.length > 1 ? Math.round(state.frameIndex * 100 / (state.frames.length - 1)) : 0}%`);
  drawSparkline("spark-throughput", "throughput_per_hour", "#6d9a59");
  drawSparkline("spark-wip", "average_wip", "#9cab67");
  drawSparkline("spark-energy", "specific_energy_kwh_per_part", "#b4a268");
}
function resetReplay() {
  pause(); state.frames = []; state.preview = null; state.frameIndex = 0;
  $("seek").disabled = true; $("play-pause").disabled = true; $("restart-playback").disabled = true;
  text("simulation-status", "等待运行"); $("simulation-status").className = "status-pill";
  text("replay-note", "真实仿真状态的采样回放。修改参数后创建新版本，从初始状态重新运行。");
  renderFrame();
}
function loadPreview(result, autoplay = false) {
  pause(); state.preview = result; state.frames = result.frames || []; state.frameIndex = 0;
  $("seek").max = String(Math.max(0, state.frames.length - 1));
  const empty = !state.frames.length;
  $("seek").disabled = empty; $("play-pause").disabled = empty; $("restart-playback").disabled = empty;
  const duration = durationLabel(result.duration_seconds);
  const total = durationLabel(result.configured_duration_seconds || activeVersion().config.until_seconds);
  text("replay-note", `实际状态采样 · 预览 ${duration}${result.truncated ? ` / 完整实验 ${total}` : ""} · ${state.frames.length} 帧。帧间可能发生多个事件；最终指标请评估完整方案。`);
  $("replay-note").title = result.description || "";
  text("simulation-status", "预览就绪"); $("simulation-status").className = "status-pill running";
  renderFrame();
  if (autoplay && !empty) play();
}
function pause() {
  state.playing = false; cancelAnimationFrame(state.animation);
  text("play-pause", "▶"); $("play-pause").setAttribute("aria-label", "播放仿真回放");
  if (state.frames.length) text("simulation-status", state.frameIndex >= state.frames.length - 1 ? "回放完成" : "回放已暂停");
}
function play() {
  if (!state.frames.length || state.adjusting) return;
  if (state.frameIndex >= state.frames.length - 1) state.frameIndex = 0;
  state.playing = true; state.lastTick = performance.now(); state.elapsed = 0;
  text("play-pause", "Ⅱ"); $("play-pause").setAttribute("aria-label", "暂停仿真回放");
  text("simulation-status", "正在回放");
  const tick = (now) => {
    if (!state.playing) return;
    state.elapsed += Math.min(250, now - state.lastTick) * Number($("playback-speed").value);
    state.lastTick = now;
    if (state.elapsed >= 100) {
      const steps = Math.floor(state.elapsed / 100); state.elapsed %= 100;
      state.frameIndex = Math.min(state.frames.length - 1, state.frameIndex + steps); renderFrame();
      if (state.frameIndex >= state.frames.length - 1) { pause(); return; }
    }
    state.animation = requestAnimationFrame(tick);
  };
  state.animation = requestAnimationFrame(tick);
}

function updateRun(run) {
  state.runs.set(run.id, run);
  const index = state.session.runs.findIndex((item) => item.id === run.id);
  const metadata = {...run}; delete metadata.result;
  if (index === -1) state.session.runs.push(metadata); else state.session.runs[index] = metadata;
  renderProgress(); updateControls();
}
function renderProgress() {
  const pending = state.session?.runs.filter((run) => run.version_id === state.session.active_version_id && ["queued", "running"].includes(run.status)) || [];
  $("run-progress").hidden = !pending.length;
  if (!pending.length) return;
  const run = pending.find((item) => item.kind === "study") || pending[0];
  text("run-progress-text", `${run.kind === "study" ? "完整方案评估" : "仿真预览"}${run.status === "queued" ? " · 等待计算" : " · 正在计算实际仿真事件"}`);
  text("run-progress-percent", "计算中");
  $("run-progress-bar").removeAttribute("value");
  if (run.kind === "preview" && !state.frames.length) {
    text("simulation-status", "计算预览中"); $("simulation-status").className = "status-pill running";
  }
}
async function watchRun(run, autoplay = true) {
  if (state.watchers.has(run.id)) return state.watchers.get(run.id);
  const sessionId = state.session.id; const generation = state.generation;
  const task = (async () => {
    let failures = 0;
    for (;;) {
      if (generation !== state.generation) return;
      try {
        run = await api(`/sessions/${sessionId}/runs/${run.id}`); failures = 0;
      } catch (error) {
        failures += 1;
        if (failures >= 4) throw new Error(`无法读取运行状态：${error.message}。刷新页面可继续恢复结果。`);
        await new Promise((resolve) => setTimeout(resolve, 1500)); continue;
      }
      if (generation !== state.generation) return;
      updateRun(run);
      if (run.status === "succeeded") {
        if (run.kind === "preview" && run.version_id === state.session.active_version_id) loadPreview(run.result, autoplay && !state.adjusting);
        renderVersions();
        if (run.kind === "study") toast("完整评估已完成，均值与置信区间已更新。");
        return run;
      }
      if (run.status === "failed") {
        if (run.kind === "preview" && run.version_id === state.session.active_version_id) {
          text("simulation-status", "运行失败"); $("simulation-status").className = "status-pill failed";
        }
        renderVersions(); throw new Error(run.error || "仿真失败，请检查参数后重试。");
      }
      await new Promise((resolve) => setTimeout(resolve, 850));
    }
  })();
  state.watchers.set(run.id, task);
  task.catch((error) => { if (generation === state.generation) showError("global-error", error); }).finally(() => {
    state.watchers.delete(run.id); if (generation === state.generation) updateControls();
  });
  return task;
}
async function startRun(kind, reuse = false) {
  const version = activeVersion();
  const existing = [...state.session.runs].reverse().find((run) => run.version_id === version.id && run.kind === kind &&
    (["queued", "running"].includes(run.status) || (reuse && run.status === "succeeded")));
  const run = existing || await api(sessionPath("/runs"), {method: "POST", body: {version_id: version.id, kind}});
  updateRun(run);
  watchRun(run).catch(() => {});
}
async function recoverRuns() {
  const generation = state.generation;
  const latestStudies = new Map();
  for (const run of state.session.runs) if (run.kind === "study" && run.status === "succeeded") latestStudies.set(run.version_id, run);
  const settled = await Promise.allSettled([...latestStudies.values()].map(async (run) => {
    const result = await api(sessionPath(`/runs/${run.id}`));
    if (generation === state.generation) state.runs.set(run.id, result);
  }));
  const missing = settled.filter((item) => item.status === "rejected");
  if (missing.length) toast(`${missing.length} 项历史结果暂时无法读取，可重新评估。`);
  if (generation !== state.generation) return;
  renderVersions();
  const pending = state.session.runs.filter((run) => ["queued", "running"].includes(run.status));
  for (const run of pending) watchRun(run, false).catch(() => {});
  if (!hasPending("preview")) {
    const latestPreview = [...state.session.runs].reverse().find((run) => run.kind === "preview" && run.status === "succeeded" && run.version_id === state.session.active_version_id);
    if (latestPreview) watchRun(latestPreview, false).catch(() => {});
  }
}
function studyForVersion(id) {
  for (const run of [...(state.session?.runs || [])].reverse()) {
    if (run.version_id === id && run.kind === "study" && run.status === "succeeded") {
      const loaded = state.runs.get(run.id);
      if (loaded?.result) return loaded.result;
    }
  }
  return null;
}
function changeDescription(change, config) {
  const path = change.path.split("."); let prefix = "";
  if (path[0] === "machines" && path.length > 2) prefix = `${config.machines[Number(path[1])]?.name || `设备 ${Number(path[1]) + 1}`} · `;
  if (path[0] === "buffers" && path.length > 2) prefix = `B${Number(path[1]) + 1} · `;
  const key = path.at(-1);
  const readable = (value) => {
    if (key === "availability" || key === "confidence_level") return `${format(Number(value) * 100, 1)}%`;
    if (value === "paper") return "论文模式"; if (value === "custom") return "自定义模式";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  };
  return `${prefix}${labels[key] || change.path}：${readable(change.before)} → ${readable(change.after)}`;
}
function renderVersions() {
  if (!state.session) return;
  const body = $("version-rows"); body.replaceChildren();
  for (const version of [...state.session.versions].reverse()) {
    const tr = node("tr"); tr.dataset.versionId = version.id;
    const first = node("td"); const title = node("div", "version-row-label", versionName(version));
    if (version.id === state.session.active_version_id) title.append(node("span", "tiny-badge", "当前"));
    first.append(title);
    const changed = version.changes || [];
    first.append(node("div", "version-detail", changed.length ? `${changed.length} 项修改 · ${version.source === "llm" ? "AI 调整" : version.source === "restore" ? "版本恢复" : "手动编辑"}` : "初始输入模型"));
    if (version.ai_config) first.append(node("div", "version-detail", aiSourceLabel(version.ai_config)));
    if (changed.length) { const short = node("div", "version-detail", changeDescription(changed[0], version.config)); first.append(short); }
    tr.append(first);
    const study = studyForVersion(version.id);
    const pending = state.session.runs.some((run) => run.version_id === version.id && run.kind === "study" && ["queued", "running"].includes(run.status));
    for (const metric of ["throughput_per_hour", "avg_wip", "specific_energy_kwh_per_part"]) {
      const cell = node("td"); const row = study?.summary?.find((item) => item.metric === metric);
      if (row) {
        cell.append(node("span", "mono", format(row.mean)));
        const interval = row.ci_low == null || row.ci_high == null ? `n=${row.n} · 区间不可估计` : `${format(row.ci_low)} – ${format(row.ci_high)}`;
        const ci = node("span", "ci", interval);
        ci.title = `${format((row.confidence_level || version.config.confidence_level) * 100, 0)}% 置信区间 · ${row.ci_method || "正态近似"} · n=${row.n}`;
        cell.append(ci);
      } else cell.append(node("span", "untested", pending ? "评估中…" : "未评估"));
      tr.append(cell);
    }
    const actions = node("td"); const buttons = node("div", "row-actions");
    const detail = node("button", "text-button", "详情"); detail.type = "button";
    detail.addEventListener("click", () => showVersion(version)); buttons.append(detail);
    if (version.id !== state.session.active_version_id) {
      const restore = node("button", "text-button version-restore", "恢复"); restore.type = "button";
      restore.addEventListener("click", action(() => restoreVersion(version))); buttons.append(restore);
    }
    actions.append(buttons); tr.append(actions); body.append(tr);
  }
  const current = studyForVersion(state.session.active_version_id);
  $("study-summary").hidden = !current;
  if (current) {
    const config = current.config || activeVersion().config;
    text("study-summary", `当前方案已完成 ${config.replications} 次独立重复 · ${durationLabel(config.until_seconds)}（含 ${durationLabel(config.warmup_seconds)}预热）· ${format(config.confidence_level * 100, 0)}% 正态近似置信区间。比较不同版本时，请同时核对时长、预热和随机种子。`);
  }
  updateControls();
}
function openInfo(title, contents) {
  text("info-title", title); $("info-body").replaceChildren(...contents); $("info-dialog").showModal();
}
function showVersion(version) {
  const intro = node("p", "", `${version.mode === "paper" ? "论文约束模式" : "自定义实验"} · ${new Date(version.created_at).toLocaleString("zh-CN")}`);
  const note = node("p", "", version.note || "模型参数与运行记录均保存在本机。");
  const list = node("ul");
  for (const change of version.changes || []) list.append(node("li", "", changeDescription(change, version.config)));
  const json = node("details"); json.append(node("summary", "text-button", "查看完整模型 JSON"), node("pre", "", JSON.stringify(version.config, null, 2)));
  const contents = [intro, note, list];
  if (version.ai_config) {
    const source = node("details");
    source.append(node("summary", "text-button", `使用的 AI 模型：${aiSourceLabel(version.ai_config)}`),
      node("pre", "", JSON.stringify(version.ai_config, null, 2)));
    contents.push(source);
  }
  openInfo(versionName(version), [...contents, json]);
}
async function restoreVersion(version) {
  state.busy = true; pause(); updateControls();
  try {
    const session = await api(sessionPath("/restore"), {method: "POST", body: {version_id: version.id, expected_version_id: state.session.active_version_id}});
    acceptSession(session); toast("已恢复为新版本，原有修改记录仍然保留。"); await startRun("preview");
  } finally { state.busy = false; updateControls(); }
}

const emptyMessages = $("messages").cloneNode(true);
function renderMessages() {
  const messages = [...(state.session?.messages || []), ...state.pendingMessages];
  const container = $("messages"); container.replaceChildren();
  if (!messages.length) { container.append(...[...emptyMessages.childNodes].map((child) => child.cloneNode(true))); return; }
  for (const message of messages) {
    const article = node("article", `message ${message.role === "user" ? "user" : message.role === "error" ? "error" : "assistant"}${message.pending ? " pending" : ""}`);
    const meta = node("div", "message-meta", message.role === "user" ? "你" : message.role === "error" ? "调整未应用" : "✳ 调整助手");
    if (message.applied === true) meta.append(node("span", "tiny-badge", "已应用"));
    if (message.applied === false) meta.append(node("span", "tiny-badge", "未修改模型"));
    article.append(meta);
    if (message.ai_config && message.role !== "user") {
      const source = node("div", "message-model", aiSourceLabel(message.ai_config));
      source.title = [message.ai_config.base_url, message.ai_config.api_format].filter(Boolean).join(" · ");
      article.append(source);
    }
    article.append(node("div", "message-content", message.content)); container.append(article);
  }
  container.scrollTop = container.scrollHeight;
}
async function adjustModel() {
  const prompt = $("prompt").value.trim(); if (!prompt) return;
  if (!state.ai?.available) { await openSettings(); toast("保存模型连接后即可发送这条提示词。"); return; }
  if (state.dirty) { toast("请先应用左侧模型修改，再让 AI 根据当前版本调整。"); return; }
  const previousHead = state.session.active_version_id;
  state.adjusting = true; state.requestAI = clone(state.ai); pause(); renderAI();
  state.pendingMessages = [{role: "user", content: prompt, pending: true}]; renderMessages();
  try {
    const session = await api(sessionPath("/adjust"), {method: "POST", body: {
      prompt, expected_version_id: previousHead, ai_profile_id: state.requestAI.profile_id,
    }});
    state.pendingMessages = []; $("prompt").value = ""; acceptSession(session);
    if (session.active_version_id !== previousHead) {
      state.adjusting = false;
      await startRun("preview", true); toast("AI 修改已通过校验，正在预览新方案。");
    } else toast("AI 已回复，本轮没有变更模型参数。");
  } catch (error) {
    state.pendingMessages = [{role: "user", content: prompt}, {role: "error", content: error.message, ai_config: state.requestAI}]; renderMessages();
  } finally { state.adjusting = false; state.requestAI = null; renderAI(); }
}

function readAIEditor() {
  return {name: $("ai-profile-name").value.trim(), model: $("ai-model").value.trim(),
    base_url: $("ai-base-url").value.trim(), api_format: $("ai-format").value,
    api_key: $("ai-api-key").value.trim(), clear_key: $("ai-clear-key").checked};
}
function retainAIDraft() {
  if (state.aiEditorId) state.aiDrafts.set(state.aiEditorId, readAIEditor());
}
function updateAIEditorControls() {
  const profile = state.aiProfiles?.profiles.find((item) => item.id === state.aiEditorId);
  const active = profile?.id === state.aiProfiles?.active_profile_id;
  text("ai-edit-status", !profile ? "新配置 · 保存后启用" : active ? "当前启用的配置" : "编辑此配置，保存后可切换使用");
  $("delete-ai-profile").disabled = state.aiSaving || !profile || active;
  $("delete-ai-profile").title = active ? "请先启用另一个配置，再删除此配置。" : "删除此模型连接配置";
  $("activate-ai-profile").disabled = state.aiSaving || !profile || active;
  $("activate-ai-profile").hidden = !profile || active;
  $("ai-api-key").disabled = state.aiSaving || $("ai-clear-key").checked;
  const endpoint = (url) => url.trim().replace(/\/+$/, "");
  const newEndpoint = profile && endpoint($("ai-base-url").value) !== endpoint(profile.base_url || "");
  text("ai-key-state", $("ai-clear-key").checked ? "保存后将移除这份配置的密钥。"
    : $("ai-api-key").value.trim() ? "新密钥将在保存后用于这份配置。"
    : newEndpoint ? "服务地址已更改，请填写新服务的密钥。"
    : profile?.available ? "此配置已有密钥；留空保留。" : "此配置尚未提供密钥，可以先保存配置。");
}
function renderAIEditor(id) {
  state.aiEditorId = id;
  const profiles = state.aiProfiles?.profiles || [];
  const select = $("ai-profile-edit-select"); select.replaceChildren();
  for (const profile of profiles) {
    const option = node("option", "", `${profile.name}${profile.id === state.aiProfiles.active_profile_id ? " · 使用中" : ""}`);
    option.value = profile.id; select.append(option);
  }
  if (id === "new") { const option = node("option", "", "新配置（尚未保存）"); option.value = "new"; select.append(option); }
  select.value = id;
  const profile = state.aiDrafts.get(id) || profiles.find((item) => item.id === id) ||
    {name: "新的模型连接", model: "gpt-4.1-mini", base_url: "https://api.openai.com/v1", api_format: "auto"};
  $("ai-profile-name").value = profile.name || "";
  $("ai-model").value = profile.model || "";
  $("ai-base-url").value = profile.base_url || "";
  $("ai-format").value = profile.api_format || "auto";
  $("ai-api-key").value = profile.api_key || "";
  $("ai-clear-key").checked = Boolean(profile.clear_key);
  $("ai-api-key").placeholder = profile.available ? "留空保留同一服务的现有密钥" : "输入该 API 服务的密钥";
  $("ai-service-preset").value = "";
  showError("settings-error", null); updateAIEditorControls();
}
function setAISettingsBusy(busy) {
  state.aiSaving = busy;
  document.querySelectorAll("#settings-form input, #settings-form select, #settings-form button").forEach((input) => { input.disabled = busy; });
  $("settings-form").setAttribute("aria-busy", String(busy));
  text("save-ai-settings", busy ? "正在保存…" : "保存并启用");
  updateAIEditorControls(); renderAI();
}
async function openSettings() {
  state.aiDrafts.clear(); showError("settings-error", null);
  $("settings-dialog").showModal(); setAISettingsBusy(true);
  try {
    acceptAIProfiles(await api("/ai/profiles"));
    renderAIEditor(state.aiProfiles.active_profile_id);
  } catch (error) { showError("settings-error", error); }
  finally { setAISettingsBusy(false); }
}
async function switchAIProfile(id) {
  if (!id || id === state.aiProfiles?.active_profile_id) return;
  state.aiSaving = true; renderAI();
  try {
    acceptAIProfiles(await api(`/ai/profiles/${encodeURIComponent(id)}/activate`, {method: "POST"}));
    toast(`已切换至 ${state.ai.profile_name}；${state.ai.available ? "下一条提示词使用此模型。" : "请先补充该服务的密钥。"}`);
  } finally { state.aiSaving = false; renderAI(); }
}
function newAIProfile(copy = false) {
  retainAIDraft();
  const draft = copy ? {...readAIEditor(), name: `${$("ai-profile-name").value.trim()} 副本`.slice(0, 60), api_key: "", clear_key: false}
    : {name: "新的模型连接", model: "", base_url: "", api_format: "auto", api_key: "", clear_key: false};
  state.aiDrafts.set("new", draft); renderAIEditor("new"); $("ai-profile-name").focus();
}
function fillAIService() {
  const preset = $("ai-service-preset").value;
  if (!preset) return;
  const templates = {
    openai: {name: "OpenAI", model: "gpt-4.1-mini", base_url: "https://api.openai.com/v1", api_format: "auto"},
    deepseek: {name: "DeepSeek", model: "deepseek-chat", base_url: "https://api.deepseek.com/v1", api_format: "chat_completions"},
    custom: {name: "自定义模型", model: "", base_url: "", api_format: "auto"},
  };
  const template = templates[preset];
  $("ai-profile-name").value = template.name; $("ai-model").value = template.model;
  $("ai-base-url").value = template.base_url; $("ai-format").value = template.api_format;
  $("ai-api-key").value = ""; $("ai-clear-key").checked = false; updateAIEditorControls();
}
async function saveSettings() {
  const draft = readAIEditor();
  const body = {name: draft.name, model: draft.model, base_url: draft.base_url, api_format: draft.api_format};
  if (draft.clear_key || draft.api_key) body.api_key = draft.clear_key ? "" : draft.api_key;
  const creating = state.aiEditorId === "new";
  const oldIds = new Set(state.aiProfiles.profiles.map((profile) => profile.id));
  setAISettingsBusy(true);
  try {
    const catalog = await api(creating ? "/ai/profiles" : `/ai/profiles/${encodeURIComponent(state.aiEditorId)}`,
      {method: creating ? "POST" : "PUT", body});
    const savedId = creating ? catalog.profiles.find((profile) => !oldIds.has(profile.id))?.id : state.aiEditorId;
    acceptAIProfiles(catalog);
    if (!savedId) throw new Error("配置已保存，但无法确认新配置，请重新打开模型接入。");
    state.aiEditorId = savedId; state.aiDrafts.clear(); $("ai-api-key").value = "";
    acceptAIProfiles(await api(`/ai/profiles/${encodeURIComponent(savedId)}/activate`, {method: "POST"}));
    $("settings-dialog").close();
    toast(state.ai.available ? "模型配置已保存并启用；连接将在请求时验证。" : "配置已保存并启用；补充密钥后即可使用 AI。");
  } finally { setAISettingsBusy(false); }
}
async function deleteAIProfile() {
  const id = state.aiEditorId;
  setAISettingsBusy(true);
  try {
    acceptAIProfiles(await api(`/ai/profiles/${encodeURIComponent(id)}`, {method: "DELETE"}));
    state.aiDrafts.delete(id); renderAIEditor(state.aiProfiles.active_profile_id);
    toast("已删除这份模型连接配置，已有实验记录仍保留。");
  } finally { setAISettingsBusy(false); }
}

const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日", "下周一"];
function weekPoint(seconds) {
  const day = Math.floor(seconds / DAY);
  const hours = Math.floor(seconds % DAY / 3600); const minutes = Math.floor(seconds % 3600 / 60);
  return `${weekdays[day]} ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}
function renderBreakSummary() {
  text("break-summary", state.breaks.length ? state.breaks.map((interval) => `${weekPoint(interval.start_second)} – ${weekPoint(interval.end_second)}`).join("；") : "无计划停产时段 · 连续运行");
}
function appendBreak(interval = {start_second: 4 * DAY + 17 * 3600, end_second: 5 * DAY + 7 * 3600}) {
  const row = node("div", "break-row");
  ["start", "end"].forEach((side, index) => {
    if (index) row.append(node("span", "", "→"));
    const seconds = interval[`${side}_second`];
    const select = node("select"); select.className = `break-${side}-day`; select.setAttribute("aria-label", `${index ? "结束" : "开始"}星期`);
    weekdays.slice(0, index ? 8 : 7).forEach((day, dayIndex) => { const option = node("option", "", day); option.value = String(dayIndex); select.append(option); });
    select.value = String(Math.floor(seconds / DAY));
    const time = node("input"); time.type = "time"; time.required = true; time.className = `break-${side}-time`;
    time.setAttribute("aria-label", `${index ? "结束" : "开始"}时间`);
    time.value = `${String(Math.floor(seconds % DAY / 3600)).padStart(2, "0")}:${String(Math.floor(seconds % 3600 / 60)).padStart(2, "0")}`;
    row.append(select, time);
  });
  const remove = node("button", "icon-button", "×"); remove.type = "button"; remove.setAttribute("aria-label", "删除停产时段");
  remove.addEventListener("click", () => row.remove()); row.append(remove); $("break-rows").append(row);
}
function editBreaks() {
  $("break-rows").replaceChildren(); state.breaks.forEach(appendBreak);
  showError("breaks-error", null); $("breaks-dialog").showModal();
}
function saveBreaks() {
  const intervals = [...document.querySelectorAll("#break-rows .break-row")].map((row) => {
    const result = {};
    for (const side of ["start", "end"]) {
      const day = Number(row.querySelector(`.break-${side}-day`).value);
      const [hours, minutes] = row.querySelector(`.break-${side}-time`).value.split(":").map(Number);
      result[`${side}_second`] = day * DAY + hours * 3600 + minutes * 60;
    }
    if (result.end_second <= result.start_second || result.end_second > 7 * DAY) throw new Error("结束时间必须晚于开始时间，且不能超过下周一 00:00。");
    return result;
  }).sort((left, right) => left.start_second - right.start_second);
  for (let index = 1; index < intervals.length; index += 1) if (intervals[index].start_second < intervals[index - 1].end_second) throw new Error("每周停产时段不能重叠。");
  state.breaks = intervals; setDirty(); renderBreakSummary(); $("breaks-dialog").close();
}
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob); const link = node("a"); link.href = url; link.download = filename;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
}
async function importConfig(event) {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("模型 JSON 文件不能超过 1 MB。");
    let config; try { config = JSON.parse(await file.text()); } catch { throw new Error("无法读取 JSON，请检查文件格式。"); }
    const imported = config.config && !config.machines ? config.config : config;
    const mode = config.mode || $("model-mode").value;
    if (!["paper", "custom"].includes(mode)) throw new Error("模型约束模式必须为 paper 或 custom。");
    state.busy = true; updateControls();
    const session = await api(sessionPath("/versions"), {method: "POST", body: {
      config: imported, mode, label: "导入模型", expected_version_id: state.session.active_version_id,
    }});
    acceptSession(session); toast("模型已通过校验并导入，正在生成预览。"); await startRun("preview");
  } finally { event.target.value = ""; state.busy = false; updateControls(); }
}
async function exportSession() {
  const response = await fetch(API + sessionPath("/export"));
  if (!response.ok) throw new Error("实验导出失败，请稍后重试。");
  downloadBlob(await response.blob(), `simlab-experiment-${state.session.id.slice(0, 8)}.zip`);
  toast("实验已导出，包含模型、版本、回放数据与评估报告。");
}
function showAssumptions() {
  const list = node("ul");
  for (const assumption of state.bootstrap?.assumptions || []) list.append(node("li", "", assumption));
  openInfo("模型说明与统计口径", [list]);
}
async function openExperiments() {
  const data = await api("/sessions");
  const list = node("div", "saved-experiments");
  for (const session of data.sessions || []) {
    const button = node("button", "saved-experiment", session.name);
    button.append(node("span", "small muted", new Date(session.created_at).toLocaleString("zh-CN")));
    button.addEventListener("click", action(async () => {
      const loaded = await api(`/sessions/${session.id}`); state.generation += 1; state.pendingMessages = []; state.runs.clear();
      acceptSession(loaded); $("info-dialog").close(); recoverRuns().catch((error) => showError("global-error", error));
    }));
    list.append(button);
  }
  openInfo("打开已有实验", [list]);
}

bind("model-form", "submit", applyModel, "model-error");
bind("run-preview", "click", async () => state.dirty ? applyModel() : startRun("preview"));
bind("run-study", "click", async () => {
  if (state.dirty) { toast("请先应用模型修改，再评估当前方案。"); return; }
  await startRun("study"); renderVersions();
});
bind("prompt-form", "submit", adjustModel);
bind("settings-form", "submit", saveSettings, "settings-error");
bind("ai-settings", "click", openSettings);
bind("manage-ai-profiles", "click", openSettings);
bind("ai-profile-select", "change", (event) => switchAIProfile(event.target.value));
bind("ai-profile-edit-select", "change", (event) => { retainAIDraft(); renderAIEditor(event.target.value); }, "settings-error");
bind("new-ai-profile", "click", () => newAIProfile(), "settings-error");
bind("copy-ai-profile", "click", () => newAIProfile(true), "settings-error");
bind("delete-ai-profile", "click", deleteAIProfile, "settings-error");
bind("activate-ai-profile", "click", async () => {
  retainAIDraft(); setAISettingsBusy(true);
  try { await switchAIProfile(state.aiEditorId); renderAIEditor(state.aiEditorId); }
  finally { setAISettingsBusy(false); }
}, "settings-error");
$("ai-service-preset").addEventListener("change", fillAIService);
$("ai-base-url").addEventListener("input", updateAIEditorControls);
$("ai-api-key").addEventListener("input", updateAIEditorControls);
$("ai-clear-key").addEventListener("change", () => { if ($("ai-clear-key").checked) $("ai-api-key").value = ""; updateAIEditorControls(); });
bind("edit-breaks", "click", editBreaks);
bind("breaks-form", "submit", saveBreaks, "breaks-error");
bind("add-break", "click", () => appendBreak());
bind("export-session", "click", exportSession);
bind("export-config", "click", () => downloadBlob(new Blob([JSON.stringify({
  schema: "manufacturing-studio-config-1", mode: $("model-mode").value, config: readDraft(),
}, null, 2)], {type: "application/json"}), "simlab-model.json"), "model-error");
bind("import-config", "click", () => $("config-file").click());
$("config-file").addEventListener("change", action(importConfig, "model-error"));
bind("show-assumptions", "click", showAssumptions);
bind("new-session", "click", async () => { await createSession(); toast("新的案例一实验已创建，旧实验仍保存在本机。"); });
bind("play-pause", "click", () => state.playing ? pause() : play());
bind("restart-playback", "click", () => { pause(); state.frameIndex = 0; renderFrame(); });
$("seek").addEventListener("input", () => { pause(); state.frameIndex = Number($("seek").value); renderFrame(); });
$("model-form").addEventListener("input", setDirty);
$("model-name").addEventListener("input", setDirty);
$("model-mode").addEventListener("change", () => { updateMode(); setDirty(); });
$("prompt").addEventListener("input", updateControls);
$("prompt").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); if (!$("send-prompt").disabled) $("prompt-form").requestSubmit(); } });
document.querySelectorAll(".prompt-chip").forEach((button) => button.addEventListener("click", () => { $("prompt").value = button.dataset.prompt; $("prompt").focus(); updateControls(); }));
document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("settings-dialog").addEventListener("close", () => { $("ai-api-key").value = ""; state.aiDrafts.clear(); });
$("settings-dialog").addEventListener("cancel", (event) => { if (state.aiSaving) event.preventDefault(); });
$("toggle-model").addEventListener("click", () => {
  const expanded = $("toggle-model").getAttribute("aria-expanded") === "true";
  $("model-body").hidden = expanded; $("toggle-model").setAttribute("aria-expanded", String(!expanded)); text("toggle-model", expanded ? "+" : "−");
});
const openSaved = node("button", "button quiet", "打开实验"); openSaved.id = "open-experiments";
openSaved.addEventListener("click", action(openExperiments)); $("new-session").before(openSaved);
let resizeTimeout;
window.addEventListener("resize", () => { clearTimeout(resizeTimeout); resizeTimeout = setTimeout(renderFrame, 100); });
document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });

async function initialize() {
  try {
    state.bootstrap = await api("/bootstrap"); state.ai = state.bootstrap.ai; renderAI();
    acceptAIProfiles(await api("/ai/profiles"));
    let saved; try { saved = localStorage.getItem(SESSION_KEY); } catch { saved = null; }
    if (saved) {
      try { acceptSession(await api(`/sessions/${saved}`)); }
      catch (error) {
        if (error.status !== 404) throw error;
        toast("之前的实验已不可用，已创建新的案例一实验。"); await createSession();
      }
    } else await createSession();
    await recoverRuns();
  } catch (error) {
    showError("global-error", `应用加载失败：${error.message}。请关闭后重新打开应用；若仍失败，请查看数据目录中的日志。`);
    text("version-badge", "连接失败");
  }
}
initialize();
