/* Pointer/keyboard graph editor. Applies through the existing input-model workflow. */
"use strict";
(() => {
  const G = window.StudioModelGraph, dialog = $("visual-model-dialog"), canvas = $("graph-canvas"), viewport = $("graph-viewport");
  const W = 154, H = 88;
  const openButton = node("button", "button", "图形建模"); openButton.type = "button"; openButton.id = "open-visual-model";
  $("edit-model").before(openButton);
  const modelButton = node("button", "button full", "拖拽编辑设备、容器与连接线"); modelButton.type = "button"; modelButton.id = "edit-model-graph";
  $("add-equipment").before(modelButton);
  let graph = null, base = null, mode = "paper", contextKey = null, sourceStamp = null, savedGraph = null;
  let selectedNode = null, selectedEdge = null, connectionFrom = null, gesture = null, pointer = null;
  let undo = [], redo = [], lastInspection = null;
  const versionKey = () => `${state.session?.id}/${state.session?.active_version_id}`;
  const unavailable = () => !state.session || state.busy || state.adjusting;
  const stale = () => contextKey !== versionKey();
  const editable = () => !unavailable() && !stale();
  const fingerprint = config => JSON.stringify([config, $("model-mode").value]);
  const graphChanged = () => graph && JSON.stringify(graph) !== savedGraph;
  const draftKey = id => "simlab.studio.graph-draft." + id;
  function saveDraft() {
    if (!graph || !contextKey) return;
    try {
      const key = draftKey(contextKey.split("/")[0]);
      if (graphChanged()) localStorage.setItem(key, JSON.stringify({graph, base, mode, contextKey, sourceStamp, savedGraph, undo, redo}));
      else localStorage.removeItem(key);
    } catch { hint("图形草稿暂不能保存到本机，请及时应用或导出模型。", true); }
  }
  function recoverDraft() {
    try {
      const saved = JSON.parse(localStorage.getItem(draftKey(state.session.id)) || "null");
      if (!saved || !Array.isArray(saved.graph?.nodes) || !Array.isArray(saved.graph?.edges) || !saved.base?.machines) return false;
      ({graph, base, mode, contextKey, sourceStamp, savedGraph} = saved);
      undo = Array.isArray(saved.undo) ? saved.undo.slice(-80) : []; redo = Array.isArray(saved.redo) ? saved.redo.slice(-80) : [];
      selectedNode = selectedEdge = connectionFrom = gesture = null; return true;
    } catch { return false; }
  }
  function conflict() {
    if (!graph) return false;
    try { return stale() || sourceStamp !== fingerprint(readDraft()); } catch { return true; }
  }
  const useDraft = node("button", "button", "保留图形并合并当前实验设置"); useDraft.id = "graph-keep-draft"; useDraft.type = "button";
  $("graph-reset").before(useDraft);
  useDraft.addEventListener("click", () => {
    try {
      if (!confirm("保留图形中的设备和连接；应用时以图形设备参数为准，并沿用当前参数表的实验设置。继续？")) return;
      const current = readDraft(); base = clone(current); mode = $("model-mode").value;
      contextKey = versionKey(); sourceStamp = fingerprint(current); render(); sync();
      hint("已保留图形和当前实验设置，请核对设备参数后应用。");
    } catch (error) { hint(error.message, true); }
  });
  const label = item => item?.data.name || "未命名";
  const hint = (message, error = false) => { text("graph-hint", message); $("graph-hint").classList.toggle("error", error); };
  const point = event => { const box = canvas.getBoundingClientRect(); return {x: event.clientX - box.left, y: event.clientY - box.top}; };
  const snap = value => Math.max(24, Math.min(4000, Math.round(value / 10) * 10));
  const layoutKey = () => "simlab.studio.graph-layout." + state.session.id;
  const topologyKey = config => JSON.stringify([config.machines.map(item => item.name), config.buffers.map(item => item.name)]);
  function readLayout(config) {
    try {
      const catalog = JSON.parse(localStorage.getItem(layoutKey()) || "[]");
      const layout = Array.isArray(catalog) && catalog.find(item => item.key === topologyKey(config));
      if (!Array.isArray(layout?.positions)) return;
      for (const item of graph.nodes) {
        const saved = layout.positions.find(entry => entry.type === item.type && entry.name === item.data.name);
        if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) { item.x = snap(saved.x); item.y = snap(saved.y); }
      }
    } catch { /* Layout storage is optional; the model remains usable. */ }
  }
  function storeLayout(config) {
    try {
      const stored = JSON.parse(localStorage.getItem(layoutKey()) || "[]"), key = topologyKey(config);
      const positions = graph.nodes.map(item => ({type: item.type, name: String(item.data.name).trim(), x: item.x, y: item.y}));
      const catalog = Array.isArray(stored) ? stored.filter(item => item?.key !== key) : [];
      localStorage.setItem(layoutKey(), JSON.stringify([{key, positions}, ...catalog].slice(0, 20))); return true;
    } catch { return false; }
  }
  function initialize(config) {
    base = clone(config); mode = $("model-mode").value; contextKey = versionKey(); sourceStamp = fingerprint(config);
    graph = G.fromConfig(config); readLayout(config); savedGraph = JSON.stringify(graph);
    undo = []; redo = []; selectedNode = null; selectedEdge = null; connectionFrom = null; gesture = null;
  }
  function open() {
    if (unavailable()) return;
    let current;
    try { current = readDraft(); }
    catch (error) { window.StudioWorkspace.openModel(); showError("model-error", error); return; }
    if (!graph || contextKey?.split("/")[0] !== state.session.id) {
      saveDraft(); if (!recoverDraft()) initialize(current);
    } else if (conflict() && !graphChanged()) initialize(current);
    render(); dialog.showModal();
    hint(conflict() ? "输入模型已变化，旧图形草稿和撤销记录已保留。请选择保留图形，或重新载入输入模型。" : "拖入设备与容器，再从输出端拖向输入端建立连接。关闭窗口会保留本次图形草稿。");
  }
  function remember(before) {
    if (JSON.stringify(before) === JSON.stringify(graph)) return;
    undo.push(before); if (undo.length > 80) undo.shift(); redo = [];
  }
  function change(fn) {
    if (!editable()) return;
    const before = clone(graph);
    try { fn(); remember(before); connectionFrom = null; render(); return true; }
    catch (error) { graph = before; render(); hint(error.message, true); return false; }
  }
  function restore(direction) {
    if (!editable()) return;
    const from = direction === "undo" ? undo : redo, to = direction === "undo" ? redo : undo;
    if (!from.length) return; to.push(clone(graph)); graph = from.pop();
    selectedNode = null; selectedEdge = null; connectionFrom = null; gesture = null; render(); hint(direction === "undo" ? "已撤销上一步。" : "已重做上一步。");
  }
  function selectNode(id, focus = false) {
    selectedNode = id; selectedEdge = null; paintSelection(); properties();
    if (focus) revealNode(id);
  }
  function revealNode(id) {
    const item = graph.nodes.find(node => node.id === id); if (!item) return;
    if (item.x < viewport.scrollLeft || item.x + W > viewport.scrollLeft + viewport.clientWidth) viewport.scrollLeft = Math.max(0, item.x - 40);
    if (item.y < viewport.scrollTop || item.y + H > viewport.scrollTop + viewport.clientHeight) viewport.scrollTop = Math.max(0, item.y - 40);
  }
  function selectEdge(id) { selectedEdge = id; selectedNode = null; connectionFrom = null; paintSelection(); properties(); }
  function paintSelection() {
    for (const item of $("graph-nodes").children) {
      const active = item.dataset.nodeId === selectedNode; item.classList.toggle("selected", active);
      item.querySelector(".graph-node-card").setAttribute("aria-pressed", String(active));
      item.classList.toggle("connecting", item.dataset.nodeId === connectionFrom);
    }
    for (const edge of $("graph-edges").querySelectorAll("[data-edge-id]")) edge.classList.toggle("selected", edge.dataset.edgeId === selectedEdge);
  }
  function edgePath(a, b) {
    const start = {x: a.x + W, y: a.y + H / 2}, end = {x: b.x - 12, y: b.y + H / 2};
    if (Math.abs(start.y - end.y) > 80) {
      const mid = (start.y + end.y) / 2;
      return `M${start.x},${start.y} H${start.x + 25} V${mid} H${end.x - 25} V${end.y} H${end.x}`;
    }
    if (end.x < start.x + 30) {
      const lane = Math.max(a.y, b.y) + H + 34;
      return `M${start.x},${start.y} H${start.x + 25} V${lane} H${end.x - 25} V${end.y} H${end.x}`;
    }
    const bend = Math.max(45, Math.abs(start.x - end.x) / 2);
    return `M${start.x},${start.y} C${start.x + bend},${start.y} ${end.x - bend},${end.y} ${end.x},${end.y}`;
  }
  function paths() {
    const svg = $("graph-edges"); svg.replaceChildren();
    const defs = svgNode("defs"), marker = svgNode("marker", {id: "graph-arrow", markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto"});
    marker.append(svgNode("path", {d: "M0 0 L8 4 L0 8z", fill: "#6b947a"})); defs.append(marker); svg.append(defs);
    const byId = new Map(graph.nodes.map(item => [item.id, item]));
    for (const edge of graph.edges) {
      const a = byId.get(edge.from), b = byId.get(edge.to); if (!a || !b) continue;
      const group = svgNode("g", {"data-edge-id": G.edgeId(edge), tabindex: 0, role: "button", "aria-label": `连接 ${label(a)} → ${label(b)}`});
      const d = edgePath(a, b);
      group.append(svgNode("path", {d, class: "graph-edge-hit"}), svgNode("path", {d, class: "graph-edge-line", "marker-end": "url(#graph-arrow)"}));
      group.addEventListener("click", event => { event.stopPropagation(); selectEdge(G.edgeId(edge)); });
      group.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); selectEdge(G.edgeId(edge)); } });
      svg.append(group);
    }
    if (connectionFrom && pointer) {
      const from = byId.get(connectionFrom);
      if (from) svg.append(svgNode("path", {d: edgePath(from, {x: pointer.x, y: pointer.y - H / 2}), class: "graph-edge-draft"}));
    }
    paintSelection();
  }
  function sizeCanvas() {
    canvas.style.width = `${Math.max(viewport.clientWidth, 950, ...graph.nodes.map(item => item.x + W + 180))}px`;
    canvas.style.height = `${Math.max(viewport.clientHeight, 620, ...graph.nodes.map(item => item.y + H + 180))}px`;
  }
  function render() {
    const focused = document.activeElement, hadFocus = dialog.contains(focused);
    const nodes = $("graph-nodes"); nodes.replaceChildren(); sizeCanvas();
    for (const item of graph.nodes) {
      const wrapper = node("div", `graph-node ${item.type}`); wrapper.dataset.nodeId = item.id;
      wrapper.style.left = `${item.x}px`; wrapper.style.top = `${item.y}px`;
      const card = node("button", "graph-node-card"); card.type = "button"; card.dataset.nodeId = item.id;
      card.setAttribute("aria-label", `${({machine: "设备", buffer: "容器", source: "原料", sink: "成品"})[item.type]} ${label(item)}`);
      card.append(node("span", "graph-node-kind", ({machine: "▣ 加工设备", buffer: "▤ 缓冲容器", source: "原料入口", sink: "成品出口"})[item.type]), node("strong", "", label(item)));
      card.append(node("small", "", item.type === "machine" ? `加工 ${item.data.cycle_time_seconds} s` : item.type === "buffer" ? `容量 ${item.data.capacity} · 转运 ${item.data.delay_seconds} s` : item.type === "source" ? "持续供料" : "统计产出"));
      card.addEventListener("click", () => selectNode(item.id));
      card.addEventListener("pointerdown", event => {
        if (!editable() || event.button !== 0 || !event.isPrimary) return;
        selectNode(item.id); const start = point(event);
        gesture = {type: "move", id: item.id, before: clone(graph), start, x: item.x, y: item.y, pointerId: event.pointerId};
        card.setPointerCapture(event.pointerId);
      });
      wrapper.append(card);
      for (const direction of ["in", "out"]) {
        if ((direction === "in" && item.type === "source") || (direction === "out" && item.type === "sink")) continue;
        const port = node("button", `graph-port ${direction}`); port.type = "button"; port.dataset.nodeId = item.id; port.dataset.port = direction;
        port.setAttribute("aria-label", `${label(item)} ${direction === "out" ? "输出端" : "输入端"}`); port.title = port.getAttribute("aria-label");
        port.addEventListener("pointerdown", event => {
          if (!editable() || direction !== "out" || event.button !== 0 || !event.isPrimary) return;
          event.stopPropagation(); connectionFrom = item.id; pointer = point(event);
          gesture = {type: "connect", pointerId: event.pointerId, start: pointer}; port.setPointerCapture(event.pointerId); paths();
          hint(`正在连接“${label(item)}”：拖到目标输入端，或点击目标输入端。Esc 取消。`);
        });
        port.addEventListener("click", event => {
          event.stopPropagation(); if (!editable()) return;
          if (direction === "out" && event.detail === 0) { connectionFrom = item.id; pointer = null; paths(); hint(`请选择“${label(item)}”要连接的输入端。`); }
          else if (direction === "in" && connectionFrom) finishConnection(item.id);
        });
        wrapper.append(port);
      }
      nodes.append(wrapper);
    }
    paths(); properties(); validate(); sync();
    if (dialog.open && hadFocus && (!focused.isConnected || document.activeElement === document.body)) viewport.focus({preventScroll: true});
  }
  function finishConnection(to) {
    const from = connectionFrom; if (!from || !editable()) return;
    change(() => G.connect(graph, from, to));
    if (!connectionFrom) hint("已添加连接线，加工顺序按连线方向确定。");
  }
  function properties() {
    const form = $("graph-properties"); form.replaceChildren();
    const item = graph?.nodes.find(node => node.id === selectedNode);
    const edge = graph?.edges.find(edge => G.edgeId(edge) === selectedEdge);
    $("graph-delete").disabled = !editable() || (!edge && (!item || ["source", "sink"].includes(item.type)));
    if (!item) {
      text("graph-selection-title", edge ? "连接线" : "选择节点或连接线");
      text("graph-selection-note", edge ? `${label(graph.nodes.find(item => item.id === edge.from))} → ${label(graph.nodes.find(item => item.id === edge.to))}。删除后可重新连线。` : "点击设备或容器编辑参数，拖动卡片调整位置。"); return;
    }
    text("graph-selection-title", label(item));
    text("graph-selection-note", ["source", "sink"].includes(item.type) ? "固定产线端点，可拖动调整位置。" : "参数随图形应用到输入模型；坐标不改变加工顺序。");
    if (["source", "sink"].includes(item.type)) return;
    const fields = item.type === "machine" ? [
      ["name", "设备名称"], ["cycle_time_seconds", "加工时间 / s", 1], ["availability", "可用率 / %", .001, 100],
      ["mttr_seconds", "平均维修时间 / s", 0], ["idle_power_kw", "空闲功率 / kW", 0, 1e9], ["processing_power_kw", "加工功率 / kW", 0, 1e9],
    ] : [["name", "容器名称"], ["capacity", "容量 / 件", 1, 10000], ["delay_seconds", "转运时间 / s", 0]];
    for (const [key, title, min, max] of fields) {
      const field = node("label", "field", title), input = node("input"); input.id = `graph-param-${key}`; input.name = key;
      input.type = key === "name" ? "text" : "number"; input.required = true;
      if (key === "name") { input.maxLength = 80; input.autocomplete = "off"; }
      else { input.min = min; if (max !== undefined) input.max = max; input.step = key === "capacity" ? "1" : "any"; }
      input.value = key === "availability" && item.data[key] !== "" ? item.data[key] * 100 : item.data[key];
      input.disabled = !editable();
      input.addEventListener("change", () => {
        if (!editable()) return;
        const before = clone(graph), raw = input.value;
        item.data[key] = key === "name" || !raw.trim() ? raw : Number(raw) / (key === "availability" ? 100 : 1);
        remember(before);
        const card = [...$("graph-nodes").children].find(node => node.dataset.nodeId === item.id)?.querySelector(".graph-node-card");
        if (card) { card.querySelector("strong").textContent = label(item); card.querySelector("small").textContent = item.type === "machine" ? `加工 ${item.data.cycle_time_seconds} s` : `容量 ${item.data.capacity} · 转运 ${item.data.delay_seconds} s`; }
        text("graph-selection-title", label(item)); validate(); sync();
      });
      field.append(input); form.append(field);
    }
  }
  function validate() {
    if (!graph) return;
    lastInspection = G.inspect(graph, base, mode, state.bootstrap.template);
    const list = $("graph-issues"); list.replaceChildren();
    $("graph-validation-status").classList.toggle("invalid", !lastInspection.ok);
    text("graph-validation-status", lastInspection.ok ? "连接完整，可应用模型。" : `有 ${lastInspection.issues.length} 项需要处理`);
    for (const issue of lastInspection.issues) {
      const li = node("li"), target = node(issue.nodeId || issue.edgeId ? "button" : "span", "", issue.message);
      if (target.tagName === "BUTTON") { target.type = "button"; target.addEventListener("click", () => issue.nodeId ? selectNode(issue.nodeId, true) : selectEdge(issue.edgeId)); }
      li.append(target); list.append(li);
    }
    const bad = new Set(lastInspection.issues.map(issue => issue.nodeId).filter(Boolean));
    for (const element of $("graph-nodes").children) element.classList.toggle("invalid", bad.has(element.dataset.nodeId));
  }
  function sync() {
    openButton.disabled = modelButton.disabled = unavailable();
    openButton.textContent = graphChanged() && !stale() ? "图形建模 · 草稿" : "图形建模";
    if (!graph) return;
    saveDraft(); useDraft.hidden = !conflict(); useDraft.disabled = unavailable();
    for (const id of ["graph-apply", "graph-run"]) $(id).disabled = !editable() || conflict() || !lastInspection?.ok;
    $("graph-undo").disabled = !editable() || !undo.length; $("graph-redo").disabled = !editable() || !redo.length;
    $("graph-arrange").disabled = !editable(); $("graph-reset").disabled = unavailable();
    $("graph-add-machine").disabled = !editable() || graph.nodes.filter(item => item.type === "machine").length >= 12;
    $("graph-add-buffer").disabled = !editable() || graph.nodes.filter(item => item.type === "buffer").length >= 11;
    text("graph-summary", `${graph.nodes.filter(item => item.type === "machine").length} 台设备 · ${graph.nodes.filter(item => item.type === "buffer").length} 个容器 · ${graph.edges.length} 条连接`);
    text("graph-save-status", stale() ? "当前方案已变化，请重新载入输入模型。" : graphChanged() ?
      `图形草稿未应用${lastInspection?.mode === "custom" && mode === "paper" ? " · 应用后转为自定义实验" : ""}` : "图形位置可自由调整，实际加工顺序由连接线决定。");
    if (!editable()) $("graph-delete").disabled = true;
    formDisabled(!editable());
  }
  function formDisabled(disabled) { $("graph-properties").querySelectorAll("input").forEach(input => { input.disabled = disabled; }); }
  function add(type, coordinates) {
    if (!change(() => { selectedNode = G.add(graph, type, snap(coordinates.x - W / 2), snap(coordinates.y - H / 2)); selectedEdge = null; })) return;
    if (selectedNode) revealNode(selectedNode);
    hint("组件已加入草稿。请连接输入端和输出端；新增设备之间需要配套容器。");
  }
  function removeSelected() {
    change(() => {
      if (selectedEdge) graph.edges = graph.edges.filter(edge => G.edgeId(edge) !== selectedEdge);
      else if (selectedNode) G.remove(graph, selectedNode);
      selectedNode = null; selectedEdge = null;
    });
  }
  async function apply(run) {
    document.activeElement?.blur();
    if (!editable()) return;
    if (sourceStamp !== fingerprint(readDraft())) throw new Error("输入模型已变化，请重新载入后再应用图形。");
    validate(); if (!lastInspection.ok) { hint("请先处理右侧标出的连接或参数问题。", true); return; }
    const {config, mode: nextMode} = lastInspection, dirty = state.dirty;
    const changed = JSON.stringify(config) !== JSON.stringify(readDraft()) || nextMode !== $("model-mode").value;
    const layoutSaved = storeLayout(config);
    renderEditor(config, nextMode); state.dirty = dirty || changed; updateControls();
    base = clone(config); mode = nextMode; sourceStamp = fingerprint(config); savedGraph = JSON.stringify(graph); sync();
    window.StudioDrafts?.save();
    dialog.close();
    if (run) await applyModel();
    else { window.StudioWorkspace.openModel(); toast(layoutSaved ? "图形已应用到输入模型。检查参数后可保存并运行。" : "模型已应用；画布位置未能保存到本机。请检查参数后运行。"); }
  }
  function cancelGesture() {
    if (gesture?.type === "move") graph = gesture.before;
    gesture = null; connectionFrom = null; pointer = null;
    if (graph) render();
  }
  window.addEventListener("pointermove", event => {
    if (!dialog.open || !editable()) return;
    const position = point(event);
    if (gesture?.type === "move" && gesture.pointerId === event.pointerId) {
      const item = graph.nodes.find(item => item.id === gesture.id); if (!item) return;
      item.x = snap(gesture.x + position.x - gesture.start.x); item.y = snap(gesture.y + position.y - gesture.start.y);
      const element = [...$("graph-nodes").children].find(node => node.dataset.nodeId === item.id);
      element.style.left = `${item.x}px`; element.style.top = `${item.y}px`; sizeCanvas(); paths();
    } else if (connectionFrom) { pointer = position; paths(); }
  });
  window.addEventListener("pointerup", event => {
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const finished = gesture; gesture = null;
    if (finished.type === "move") { remember(finished.before); validate(); sync(); }
    else {
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.graph-port.in');
      if (target && dialog.contains(target)) finishConnection(target.dataset.nodeId);
      else if (Math.hypot(point(event).x - finished.start.x, point(event).y - finished.start.y) > 5) { connectionFrom = null; paths(); hint("连接未添加，请从输出端拖到目标输入端。", true); }
    }
  });
  window.addEventListener("pointercancel", () => { if (gesture) cancelGesture(); });
  viewport.addEventListener("dragover", event => {
    if (editable() && [...event.dataTransfer.types].includes("application/x-simlab-component")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
  });
  viewport.addEventListener("drop", event => {
    const type = event.dataTransfer.getData("application/x-simlab-component");
    if (!editable() || !["machine", "buffer"].includes(type)) return;
    event.preventDefault(); add(type, point(event));
  });
  for (const type of ["machine", "buffer"]) {
    const button = $(`graph-add-${type}`);
    button.addEventListener("dragstart", event => {
      if (!editable() || button.disabled) { event.preventDefault(); return; }
      event.dataTransfer.setData("application/x-simlab-component", type); event.dataTransfer.effectAllowed = "copy";
    });
    button.addEventListener("click", () => add(type, {x: viewport.scrollLeft + 120, y: viewport.scrollTop + 150}));
  }
  dialog.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Escape" && (connectionFrom || gesture)) { event.preventDefault(); event.stopPropagation(); cancelGesture(); hint("已取消拖动或连线。"); return; }
    if (event.target.closest("input,textarea,select")) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); restore(event.shiftKey ? "redo" : "undo"); }
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") { event.preventDefault(); restore("redo"); }
    else if (event.key === "Delete" && !$("graph-delete").disabled) { event.preventDefault(); removeSelected(); }
    else if (event.target.closest(".graph-node-card") && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault(); const id = event.target.closest(".graph-node-card").dataset.nodeId;
      const step = event.shiftKey ? 40 : 10;
      change(() => { const item = graph.nodes.find(item => item.id === id); item.x = snap(item.x + (event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0)); item.y = snap(item.y + (event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0)); });
      [...$("graph-nodes").children].find(item => item.dataset.nodeId === id)?.querySelector(".graph-node-card").focus({preventScroll: true});
    }
  });
  dialog.addEventListener("cancel", event => {
    if (connectionFrom || gesture) { event.preventDefault(); cancelGesture(); hint("已取消拖动或连线。"); }
  });
  dialog.addEventListener("close", () => { cancelGesture(); sync(); });
  $("graph-properties").addEventListener("submit", event => { event.preventDefault(); document.activeElement?.blur(); });
  $("close-visual-model").addEventListener("click", () => dialog.close());
  $("graph-undo").addEventListener("click", () => restore("undo")); $("graph-redo").addEventListener("click", () => restore("redo"));
  $("graph-delete").addEventListener("click", removeSelected);
  $("graph-arrange").addEventListener("click", () => change(() => { const result = G.inspect(graph, base, mode, state.bootstrap.template); G.arrange(graph, result.ok ? result.order : undefined); }));
  $("graph-reset").addEventListener("click", () => { try {
    if (graphChanged() && !confirm("重新载入将放弃当前图形草稿及撤销记录，是否继续？")) return;
    initialize(readDraft()); render(); hint("已重新载入当前输入模型。");
  } catch (error) { hint(error.message, true); } });
  for (const button of [openButton, modelButton]) button.addEventListener("click", action(open, "global-error"));
  for (const [id, run] of [["graph-apply", false], ["graph-run", true]]) $(id).addEventListener("click", async () => {
    try { await apply(run); }
    catch (error) {
      if (dialog.open) hint(error.message, true);
      else { window.StudioWorkspace.openModel(); showError("model-error", error); }
    }
  });
  window.StudioVisualEditor = {sync, hasDraft: () => {
    if (graphChanged() && contextKey?.split("/")[0] === state.session?.id) return true;
    try { return Boolean(state.session && localStorage.getItem(draftKey(state.session.id))); } catch { return false; }
  }}; sync();
  window.addEventListener("beforeunload", saveDraft);
})();
