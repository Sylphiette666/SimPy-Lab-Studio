/* Experiment tools: all imports and selected batch candidates require an explicit action. */
"use strict";
(() => {
  function panel(title, id) {
    $(id)?.close(); $(id)?.remove();
    const dialog = node("dialog", "modal wide-modal productivity"); dialog.id = id;
    const heading = node("div", "modal-heading");
    heading.append(node("h2", "", title), button("关闭", () => dialog.close()));
    const error = node("div", "notice error"); error.id = id + "-error"; error.hidden = true; error.setAttribute("role", "alert");
    const body = node("div", "tool-body"); dialog.append(heading, error, body);
    document.body.append(dialog); dialog.showModal(); return {dialog, body};
  }
  function button(label, fn, id) {
    const el = node("button", "button", label); el.type = "button"; if (id) el.id = id;
    el.addEventListener("click", async () => {
      const dialog = el.closest("dialog"), error = dialog ? dialog.id + "-error" : "global-error";
      const target = $(error) ? error : "global-error";
      showError(target, null); el.disabled = true;
      try { await fn(); } catch (e) { showError(target, e); }
      finally { if (el.isConnected) el.disabled = false; }
    }); return el;
  }
  function field(label, value = "", type = "text") {
    const wrap = node("label", "field", label), input = node("input"); input.type = type; input.value = value;
    wrap.append(input); return {wrap, input};
  }
  function selectField(label, options, value) {
    const wrap = node("label", "field", label), input = node("select");
    for (const [key, text] of options) { const opt = node("option", "", text); opt.value = key; input.append(opt); }
    if (value !== undefined) input.value = value; wrap.append(input); return {wrap, input};
  }
  function table(headers, rows) {
    const outer = node("div", "tool-table"), table = node("table"), head = node("thead"), tr = node("tr");
    headers.forEach(h => tr.append(node("th", "", h))); head.append(tr); table.append(head);
    const body = node("tbody");
    rows.forEach(row => { const tr = node("tr"); row.forEach(value => { const td = node("td"); td.append(value instanceof Node ? value : document.createTextNode(String(value ?? "—"))); tr.append(td); }); body.append(tr); });
    table.append(body); outer.append(table); return outer;
  }
  async function request(path, options = {}) {
    const response = await fetch(API + path, {...options, headers: {"Content-Type": "application/json"}, body: options.body === undefined ? undefined : JSON.stringify(options.body)});
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail;
      const message = typeof detail === "string" ? detail : detail?.message || "请求未完成。";
      const errors = detail?.rows || detail?.errors || data.errors || [];
      throw new Error(message + (errors.length ? "\n" + errors.map(e => `${e.row ? "第 " + e.row + " 行" : e.path || ""}：${e.message}`).join("\n") : ""));
    }
    return data;
  }
  async function encodeFile(file, max = 1000000) {
    if (file.size > max) throw new Error(`文件过大，最多 ${Math.round(max/1000000)} MB。`);
    const bytes = new Uint8Array(await file.arrayBuffer()); let binary = "";
    for (let i=0; i<bytes.length; i+=16384) binary += String.fromCharCode(...bytes.subarray(i, i+16384));
    return btoa(binary);
  }
  async function download(path, filename, options = {}) {
    const response = await fetch(API + path, options); if (!response.ok) throw new Error("文件导出失败，请稍后重试。");
    downloadBlob(await response.blob(), filename);
  }
  const num = value => Number.isFinite(value) ? format(value, 4) : "—";
  function parameterLabel(path, config) {
    const parts = path.split(".");
    return parts.length === 3 && config[parts[0]]?.[parts[1]]
      ? `${config[parts[0]][parts[1]].name} · ${labels[parts[2]] || parts[2]}` : labels[path] || path;
  }

  // Parameter explanations and actionable validation, including coupled fields.
  const hints = {
    cycle_time_seconds: "单位：秒，至少 1 秒。单件加工时间，越小表示加工越快。",
    availability: "单位：%，范围大于 0 至 100。小于 100% 时平均维修时间必须大于 0。",
    mttr_seconds: "单位：秒，至少 0。发生故障时的平均修复时长。",
    idle_power_kw: "单位：kW，范围 0–10⁹。非加工状态使用此功率。",
    processing_power_kw: "单位：kW，范围 0–10⁹。设备加工状态的功率。",
    capacity: "单位：件，范围 1–10000 的整数；论文模式固定为 5。",
    delay_seconds: "单位：秒，至少 0。物料进入容器后的转运延迟。",
  };
  const issues = node("div", "notice parameter-issues"); issues.id = "parameter-issues"; issues.hidden = true; $("model-form").prepend(issues);
  function validateFields() {
    const inputs = [...document.querySelectorAll("#model-form input[type=number]")];
    for (const input of inputs) { input.setCustomValidity(""); input.removeAttribute("aria-invalid"); }
    const warm = $("warmup-days"), until = $("until-days");
    if (warm.value && until.value && Number(warm.value) >= Number(until.value)) warm.setCustomValidity("预热时长必须小于仿真时长。");
    inputs.filter(el => /-availability$/.test(el.id)).forEach(el => {
      const mttr = $(el.id.replace("availability", "mttr_seconds"));
      if (Number(el.value) < 100 && Number(mttr.value) <= 0) mttr.setCustomValidity("可用率不足 100% 时，平均维修时间必须大于 0。");
    });
    issues.replaceChildren();
    const invalid = inputs.filter(input => !input.disabled && !input.validity.valid);
    issues.hidden = !invalid.length;
    if (invalid.length) issues.append(node("strong", "", `还有 ${invalid.length} 项参数需要检查`));
    for (const input of invalid) {
      input.setAttribute("aria-invalid", "true");
      const title = input.closest("label")?.firstChild?.textContent || input.id;
      const label = `${title}：${input.validationMessage}`;
      issues.append(button(label, () => { const details = input.closest("details"); if (details) details.open = true; input.focus(); input.scrollIntoView({block: "center"}); }));
    }
  }
  function annotate() {
    for (const input of document.querySelectorAll("#machine-fields input, #buffer-fields input")) {
      const key = input.id.replace(/^(machine|buffer)-\d+-/, "");
      if (!hints[key] || input.dataset.explained) continue;
      input.dataset.explained = "true";
      if (key.endsWith("power_kw")) input.max = "1000000000";
      const help = node("small", "field-help", hints[key]); help.id = input.id + "-help";
      input.parentElement.append(help); input.setAttribute("aria-describedby", help.id);
    }
    validateFields();
  }
  new MutationObserver(annotate).observe($("machine-fields"), {childList: true, subtree: true});
  new MutationObserver(annotate).observe($("buffer-fields"), {childList: true, subtree: true});
  $("model-form").addEventListener("input", validateFields);
  $("model-mode").addEventListener("change", validateFields);
  $("model-form").addEventListener("invalid", event => { const details = event.target.closest("details"); if (details) details.open = true; }, true);

  // Connection test uses the editor snapshot without changing the saved profile.
  const testButton = button("测试连接", async () => {
    const draft = readAIEditor(), signature = JSON.stringify(draft);
    const body = {name: draft.name || "连接测试", model: draft.model, base_url: draft.base_url, api_format: draft.api_format};
    if (state.aiEditorId !== "new") body.profile_id = state.aiEditorId;
    if (draft.clear_key || draft.key_source !== "stored" && draft.api_key) body.api_key = draft.clear_key ? "" : draft.api_key;
    testMessage.textContent = "正在测试当前地址、密钥、模型和协议…";
    try {
      const result = await request("/ai/test", {method: "POST", body});
      testMessage.textContent = $("settings-dialog").open && JSON.stringify(readAIEditor()) === signature
        ? `${result.ok ? "✓ " : ""}${result.message} 耗时 ${result.latency_ms} ms${result.total_tokens != null ? `，用量 ${result.total_tokens} tokens` : ""}。`
        : "配置已变化，请重新测试。";
    } catch (error) { testMessage.textContent = error.message; }
  }, "test-ai-connection");
  const testMessage = node("p", "small muted", "测试会向所选服务发送一个简短请求，可能产生少量费用，不修改模型配置。"); testMessage.id = "connection-test-result"; testMessage.setAttribute("aria-live", "polite");
  $("settings-form").querySelector(".modal-actions").prepend(testButton);
  $("settings-form").querySelector(".modal-actions").before(testMessage);
  $("settings-form").addEventListener("input", () => { testMessage.textContent = "配置已编辑，可重新测试连接；测试可能产生少量费用。"; });
  $("settings-dialog").addEventListener("close", () => { testMessage.textContent = "测试会发送一个简短请求，可能产生少量费用。"; });

  async function importTable() {
    const original = readDraft(), mode = $("model-mode").value;
    const {dialog, body} = panel("表格参数导入", "table-import-dialog");
    body.append(node("p", "muted", "下载当前模型的 CSV 模板，可用 Excel 编辑并另存为 CSV 或单工作表 XLSX。按当前产线顺序填写序号；空白参数保留原值。可用率以百分数填写，例如 90。导入先预览，再写入未保存草稿。"));
    body.append(button("下载当前参数模板", () => download("/tables/template", "模型参数模板.csv", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(original)}), "download-parameter-template"));
    const file = field("选择 CSV / XLSX 文件", "", "file"); file.input.accept = ".csv,.xlsx"; file.input.id = "parameter-table-file"; body.append(file.wrap);
    const content = node("div"); body.append(content);
    file.input.addEventListener("change", async () => {
      content.replaceChildren(); if (!file.input.files[0]) return;
      try {
        const chosen = file.input.files[0], data = await encodeFile(chosen);
        const payload = {filename: chosen.name, data};
        const inspected = await request("/tables/preview", {method: "POST", body: payload});
        const map = node("div", "tool-grid"), mappings = new Map();
        for (const header of inspected.columns) {
          const field = selectField(header + " →", [["", "忽略此列"], ...Object.entries(inspected.fields)], inspected.mapping[header]);
          field.input.dataset.column = header; mappings.set(header, field.input); map.append(field.wrap);
        }
        content.append(node("h3", "", "核对列映射"), map, table(inspected.columns, inspected.rows.slice(0, 6)));
        const preview = node("div");
        const previewButton = button("检查并预览改动", async () => {
          preview.replaceChildren();
          const mapping = Object.fromEntries([...mappings].map(([key, el]) => [key, el.value]));
          const result = await request("/tables/preview", {method: "POST", body: {...payload, config: original, mode, mapping}});
          preview.append(node("h3", "", `${result.changes.length} 项改动`), table(["参数", "原值", "新值"], result.changes.map(change => [parameterLabel(change.path, original), change.before, change.after])));
          preview.append(button("确认导入到参数草稿", () => {
            renderEditor(result.config, mode); state.dirty = true; updateControls(); StudioDrafts.save();
            dialog.close(); StudioWorkspace.openModel(); toast("参数已导入草稿，核对后点击应用修改并预览。");
          }, "apply-table-import"));
        }, "preview-table-import");
        map.addEventListener("change", () => preview.replaceChildren());
        content.append(previewButton, preview);
      } catch (error) { showError("table-import-dialog-error", error); }
    });
  }
  $("import-config").after(button("表格导入", importTable, "import-parameter-table"));

  async function experiments() {
    const {dialog, body} = panel("实验管理与备份", "experiments-dialog");
    const search = field("搜索名称或标签"), scope = selectField("范围", [["active", "未归档"], ["archived", "已归档"], ["all", "全部"]]);
    search.input.id = "experiment-search"; scope.input.id = "experiment-scope";
    const filters = node("div", "tool-grid"); filters.append(search.wrap, scope.wrap); body.append(filters);
    const list = node("div"); body.append(list);
    let catalog = (await request("/sessions")).sessions;
    function render() {
      list.replaceChildren();
      const rows = catalog.filter(s => (scope.input.value === "all" || s.archived === (scope.input.value === "archived")) && `${s.name} ${(s.tags || []).join(" ")}`.toLowerCase().includes(search.input.value.trim().toLowerCase()));
      if (!rows.length) { list.append(node("p", "muted", "没有符合条件的实验。")); return; }
      list.append(table(["实验名称", "标签（逗号分隔）", "操作"], rows.map(s => {
        const name = field("", s.name), tags = field("", (s.tags || []).join(", "));
        name.input.maxLength = 160; name.input.setAttribute("aria-label", "实验名称"); tags.input.setAttribute("aria-label", "实验标签");
        const controls = node("div", "tool-actions");
        async function save(archived) {
          await request(`/sessions/${s.id}/metadata`, {method: "PUT", body: {display_name: name.input.value.trim(), tags: tags.input.value.split(/[,，]/).map(t => t.trim()).filter(Boolean), archived}});
          catalog = (await request("/sessions")).sessions; render();
        }
        controls.append(button("打开", async () => {
          StudioDrafts.save(); pause(); await StudioStorage.prepare(s.id);
          const loaded = await request(`/sessions/${s.id}`); state.generation += 1; state.pendingMessages = []; state.runs.clear();
          acceptSession(loaded); dialog.close(); await recoverRuns();
        }), button("保存名称/标签", () => save(s.archived)), button(s.archived ? "取消归档" : "归档", () => save(!s.archived)));
        return [name.input, tags.input, controls];
      })));
    }
    search.input.addEventListener("input", render); scope.input.addEventListener("change", render); render();
    const backupSection = node("section", "tool-section"), backupList = node("div"), restoreArea = node("div");
    backupSection.append(node("h3", "", "实验备份与恢复"), node("p", "muted", "备份包含实验、模型版本、对话、已完成结果与草稿，不含 API 配置和密钥。自动备份在软件运行期间每 24 小时检查一次，保留最近 7 份。恢复会创建新实验，原实验继续保留。"));
    const auto = field("运行期间每天自动备份", "", "checkbox"); auto.input.id = "automatic-backup";
    const backupState = await request("/backups"); auto.input.checked = backupState.automatic_backup;
    auto.input.addEventListener("change", async () => {
      try { await request("/workspace", {method: "PUT", body: {automatic_backup: auto.input.checked}}); if (auto.input.checked) { await request("/backups", {method: "POST"}); await refreshBackups(); } }
      catch (e) { showError("experiments-dialog-error", e); }
    });
    async function previewRestore(data) {
      restoreArea.replaceChildren();
      const preview = await request("/restore", {method: "POST", body: {data}});
      restoreArea.append(table(["待恢复实验", "版本数", "运行数"], preview.sessions.map(s => [s.name, s.versions, s.runs])), button("确认恢复为新实验", async () => {
        const result = await request("/restore", {method: "POST", body: {data, apply: true}});
        restoreArea.replaceChildren(node("p", "notice", `已恢复 ${result.session_ids.length} 个实验。`));
        catalog = (await request("/sessions")).sessions; scope.input.value = "all"; search.input.value = ""; render();
      }, "confirm-restore-experiments"));
    }
    async function refreshBackups() {
      const data = await request("/backups"); backupList.replaceChildren();
      if (data.backup_error) backupList.append(node("p", "notice error", data.backup_error));
      backupList.append(table(["备份时间", "大小", "操作"], data.items.map(item => {
        const actions = node("div", "tool-actions"); actions.append(button("下载", () => download(`/backups/${item.name}`, item.name)), button("预览恢复", async () => {
          const response = await fetch(API + `/backups/${item.name}`); if (!response.ok) throw new Error("无法读取备份。");
          await previewRestore(await encodeFile(await response.blob(), 25000000));
        })); return [new Date(item.created_at).toLocaleString("zh-CN"), `${num(item.bytes/1024)} KB`, actions];
      })));
    }
    const zip = field("从实验导出包或备份 ZIP 恢复", "", "file"); zip.input.accept = ".zip"; zip.input.id = "restore-experiment-file";
    zip.input.addEventListener("change", async () => { try { if (zip.input.files[0]) await previewRestore(await encodeFile(zip.input.files[0], 25000000)); } catch (e) { showError("experiments-dialog-error", e); } });
    backupSection.append(auto.wrap, button("立即备份全部实验", async () => { await request("/backups", {method: "POST"}); await refreshBackups(); }, "backup-experiments"), button("导出窗口冲突草稿", () => downloadBlob(new Blob([JSON.stringify(StudioStorage.conflicts(), null, 2)], {type: "application/json"}), "草稿冲突副本.json")), backupList, zip.wrap, restoreArea);
    body.append(backupSection); await refreshBackups();
  }

  async function diagnostics() {
    const {body} = panel("瓶颈与评估可信度", "analysis-dialog"), sid = state.session.id;
    const options = state.session.versions.map(v => [v.id, versionName(v)]);
    const selected = selectField("分析方案", options, $("result-version-select").value || state.session.active_version_id);
    const baseline = selectField("对照方案（可选）", [["", "不比较"], ...options]);
    const precision = field("目标相对半宽（%）", "5", "number"); precision.input.min = "0.1"; precision.input.max = "50"; precision.input.step = "0.1";
    const controls = node("div", "tool-grid"); controls.append(selected.wrap, baseline.wrap, precision.wrap); body.append(controls);
    const output = node("div"); body.append(button("分析完整评估结果", async () => {
      const value = Number(precision.input.value)/100;
      const result = await request(`/sessions/${sid}/analysis/${selected.input.value}?precision=${value}${baseline.input.value ? `&baseline=${baseline.input.value}` : ""}`);
      output.replaceChildren(node("p", "small muted", result.note));
      output.append(node("h3", "", `${num(result.confidence_level*100)}% t 置信区间与重复次数建议`), table(["指标", "均值", "置信区间", "有效重复", "建议总重复次数"], result.metrics.map(row => [row.label, `${num(row.mean)} ${row.unit}`, `${num(row.ci_low)} ～ ${num(row.ci_high)}`, `${row.n}（缺失 ${row.missing}）`, row.recommended_replications === null ? "无法估计（不足两次或均值为零）" : row.recommended_replications > 50 ? "超过 50，需调整实验设计" : row.std === 0 ? "观测方差为零，请另加种子核查" : row.recommended_replications])));
      if (baseline.input.value) {
        output.append(node("h3", "", "配对比较：分析方案 − 对照方案"));
        if (!result.comparable) output.append(node("p", "notice", "评估条件不一致，未进行配对检验。请统一时长、预热、随机种子及置信水平。"));
        else output.append(table(["指标", "配对数", "平均差", "差值区间", "p 值", "结论"], result.metrics.map(row => [row.label, row.paired.n, num(row.paired.mean), `${num(row.paired.ci_low)} ～ ${num(row.paired.ci_high)}`, num(row.paired.p_value), row.paired.conclusion])));
      }
      output.append(node("h3", "", "设备时间分解（排除预热）"));
      const states = [["processing", "加工"], ["blocked", "阻塞"], ["starved", "缺料"], ["failed", "故障"], ["off_shift", "停班"]];
      output.append(table(["设备", "状态占比", ...states.map(s => s[1] + " / 小时")], result.machines.map(machine => {
        const bar = node("div", "state-stack"); states.forEach(([key, label]) => { const segment = node("span", `state-${key}`); segment.style.width = `${machine.fractions[key]*100}%`; segment.title = `${label} ${num(machine.fractions[key]*100)}%`; bar.append(segment); });
        return [machine.name, bar, ...states.map(([key]) => num(machine.seconds[key]/3600))];
      })));
      const busiest = [...result.machines].sort((a,b) => b.fractions.processing-a.fractions.processing)[0];
      if (busiest) output.append(node("p", "muted", `${busiest.name} 的加工占比最高，可优先检查其能力；高阻塞提示下游约束，高缺料提示上游供给不足。占比排序是诊断线索，需通过参数实验验证瓶颈。`));
      output.append(node("h3", "", "缓冲占用热力图 · 每格代表一次重复"));
      for (const buffer of result.buffers) {
        const line = node("div", "heat-row"); line.append(node("strong", "", `${buffer.name} · 平均 ${num(buffer.mean_fill*100)}%`));
        const cells = node("div", "heat-cells"); buffer.replication_fill.forEach((fill, i) => { const cell = node("span", "heat-cell", String(i+1)); cell.style.background = `rgba(29,117,96,${0.12+0.88*Math.min(1,Math.max(0,fill))})`; cell.title = `第 ${i+1} 次：平均占用 ${num(fill*100)}%（容量 ${buffer.capacity}）`; cells.append(cell); }); line.append(cells); output.append(line);
      }
      output.append(button("导出分析数据", () => downloadBlob(new Blob([JSON.stringify(result, null, 2)], {type: "application/json"}), "实验诊断.json")));
    }, "run-diagnostics"), output);
  }
  $("run-study").after(button("瓶颈与可信度", diagnostics, "open-diagnostics"));

  async function batchExperiments() {
    const sid = state.session.id, version = activeVersion();
    const {dialog, body} = panel("批量参数实验", "batch-dialog");
    body.append(node("p", "muted", `基于 ${versionName(version)}，每组沿用 ${durationLabel(version.config.until_seconds)}、${version.config.replications} 次重复及相同种子。最多 3 个参数、16 个组合。任务按顺序计算，可关闭此窗口后重新查看。`));
    const parameters = await request(`/sessions/${sid}/parameters`), gridArea = node("div"), grid = [];
    const parameterOptions = parameters.map(p => [p.path, `${p.name} · ${labels[p.field] || p.field}${p.field === "availability" ? "（%）" : ""}`]);
    function addGrid() {
      if (grid.length >= 3) return;
      const select = selectField("扫描参数", parameterOptions), values = field("取值（逗号分隔）", String(parameters[0].value));
      select.input.addEventListener("change", () => { const p = parameters.find(p => p.path === select.input.value); values.input.value = String(p.value*(p.field === "availability" ? 100 : 1)); });
      const row = node("div", "tool-grid"), item = {select: select.input, values: values.input, row};
      row.append(select.wrap, values.wrap, button("移除", () => { grid.splice(grid.indexOf(item), 1); row.remove(); })); grid.push(item); gridArea.append(row);
    }
    addGrid(); body.append(gridArea, button("添加扫描参数", addGrid));
    const targets = node("details"), targetFields = {};
    targets.append(node("summary", "", "用实际生产指标校准（可选）"), node("p", "small muted", "填写与仿真统计口径一致的正数指标。按相对误差均方根排序，仅表示本批参数与实测值的接近程度；建议保留另一组实际数据做验证。"));
    for (const [key, label] of [["throughput_per_hour", "实测产出率（件/小时）"], ["avg_wip", "实测平均在制品（件）"], ["specific_energy_kwh_per_part", "实测单位能耗（kWh/件）"]]) {
      const entry = field(label, "", "number"); entry.input.min = "0"; entry.input.step = "any"; targetFields[key] = entry.input; targets.append(entry.wrap);
    }
    body.append(targets);
    const selector = selectField("已保存的批量任务", [["", "选择任务"]]), status = node("p", "notice"), results = node("div");
    selector.input.id = "batch-job-select";
    const sorting = selectField("结果排序", [["throughput_per_hour", "产出率从高到低"], ["avg_wip", "在制品从低到高"], ["specific_energy_kwh_per_part", "单位能耗从低到高"], ["calibration_error", "实测匹配误差从低到高"]]);
    let job = null, timer = null, signature = null, requestId = null;
    const jobNames = {queued: "排队中", running: "计算中", cancelling: "将在本次重复完成后取消", cancelled: "已取消", interrupted: "重启中断", failed: "失败", succeeded: "已完成"};
    async function refreshList(selected) {
      const jobs = await request(`/sessions/${sid}/batches`); selector.input.replaceChildren(node("option", "", "选择任务")); selector.input.firstChild.value = "";
      for (const item of jobs.reverse()) { const option = node("option", "", `${new Date(item.created_at).toLocaleString("zh-CN")} · ${jobNames[item.status] || item.status}`); option.value = item.id; selector.input.append(option); }
      if (selected) selector.input.value = selected;
    }
    function renderJob() {
      if (!job) return;
      const names = jobNames;
      const option = [...selector.input.options].find(item => item.value === job.id);
      if (option) option.textContent = `${new Date(job.created_at).toLocaleString("zh-CN")} · ${names[job.status] || job.status}`;
      status.textContent = `${names[job.status]} · ${job.completed_replications}/${job.total_replications} 次重复${job.error ? " · " + job.error : ""}`;
      const key = sorting.input.value, value = row => key === "calibration_error" ? row.calibration_error : row.means?.[key];
      const rows = [...job.scenarios].sort((a,b) => value(a) == null ? 1 : value(b) == null ? -1 : (value(a)-value(b))*(key === "throughput_per_hour" ? -1 : 1));
      results.replaceChildren(table(["方案 / 参数", "状态", "产出率", "在制品", "单位能耗", "实测误差", "操作"], rows.map(row => {
        const adopt = button("采用此方案", async () => {
          if (state.session.id !== sid) throw new Error("当前实验已切换，请重新打开批量实验。");
          const saved = await request(`/sessions/${sid}/versions`, {method: "POST", body: {config: row.config, mode: job.mode, label: `批量实验方案 ${row.index+1}`, expected_version_id: job.base_version_id}});
          acceptSession(saved); dialog.close(); await startRun("preview");
        });
        adopt.disabled = row.status !== "succeeded" || job.base_version_id !== state.session.active_version_id;
        return [`${row.index+1} · ${Object.entries(row.parameters).map(([path,value]) => { const p = parameters.find(p => p.path === path); return `${p?.name} ${labels[p?.field] || path}=${p?.field === "availability" ? num(value*100)+"%" : value}`; }).join("；")}`, names[row.status] || row.status, num(row.means?.throughput_per_hour), num(row.means?.avg_wip), num(row.means?.specific_energy_kwh_per_part), row.calibration_error == null ? "未设置" : num(row.calibration_error*100)+"%", adopt];
      })));
      if (job.base_version_id !== state.session.active_version_id) results.append(node("p", "notice", "当前模型版本已变化，此任务结果仅供查看，不能覆盖当前版本。"));
    }
    async function loadJob(id) {
      clearTimeout(timer); if (!id) return;
      job = await request(`/sessions/${sid}/batches/${id}`); renderJob();
      if (dialog.open && ["queued", "running", "cancelling"].includes(job.status)) timer = setTimeout(() => loadJob(id).catch(e => showError("batch-dialog-error", e)), 700);
    }
    const start = button("提交批量实验", async () => {
      const selections = {}, actual = {};
      for (const item of grid) {
        if (Object.hasOwn(selections, item.select.value)) throw new Error("同一参数只能选择一次。");
        const pieces = item.values.value.split(/[,，]/).map(v => v.trim());
        if (!pieces.length || pieces.some(v => !v || !Number.isFinite(Number(v)))) throw new Error("请用逗号分隔有效数值。");
        selections[item.select.value] = pieces.map(v => Number(v)/(item.select.value.endsWith(".availability") ? 100 : 1));
      }
      for (const [key, input] of Object.entries(targetFields)) if (input.value.trim()) actual[key] = Number(input.value);
      const payload = {expected_version_id: version.id, grid: selections, targets: actual};
      const next = JSON.stringify(payload); if (signature !== next) { signature = next; requestId = crypto.randomUUID(); }
      const created = await request(`/sessions/${sid}/batches`, {method: "POST", body: {...payload, request_id: requestId}});
      await refreshList(created.id); await loadJob(created.id);
    }, "start-batch-experiment");
    const actions = node("div", "tool-actions"); actions.append(start, button("取消当前任务", async () => { if (job) { await request(`/sessions/${sid}/batches/${job.id}/cancel`, {method: "POST"}); await loadJob(job.id); } }, "cancel-batch-experiment"), button("导出结果 CSV", () => { if (job) return download(`/sessions/${sid}/batches/${job.id}/export`, "批量实验结果.csv"); }));
    body.append(actions, selector.wrap, status, sorting.wrap, results); await refreshList();
    selector.input.addEventListener("change", () => loadJob(selector.input.value).catch(e => showError("batch-dialog-error", e)));
    sorting.input.addEventListener("change", renderJob); dialog.addEventListener("close", () => clearTimeout(timer));
  }
  $("new-session").after(button("批量实验", batchExperiments, "open-batch-experiments"));
  window.StudioProductivity = {experiments};
})();
