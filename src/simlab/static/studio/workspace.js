/* Presentation and workflow only. The existing simulation and API remain unchanged. */
"use strict";

(() => {
  const requestedStudies = new Set();
  let completedVersion = null;
  let wasFullscreen = false;
  let reportText = [];
  let resultSelection = null;
  const metrics = [
    {key: "throughput_per_hour", label: "产出率", unit: "件 / 小时", direction: 1},
    {key: "avg_wip", label: "平均在制品", unit: "件 · 时间加权", direction: -1},
    {key: "specific_energy_kwh_per_part", label: "单位产品能耗", unit: "kWh / 件", direction: -1},
  ];
  const key = () => state.session ? `${state.session.id}/${state.session.active_version_id}` : null;

  function immersive(enabled) {
    document.body.classList.toggle("immersive", enabled);
    $("workspace-exit").hidden = !enabled;
    text("fullscreen-toggle", enabled ? "退出全屏" : "全屏预览");
    $("fullscreen-toggle").setAttribute("aria-pressed", String(enabled));
    requestAnimationFrame(renderFrame);
  }
  function enterFullscreen() {
    immersive(true);
    // Request synchronously in the click/submit handler; calculations are asynchronous.
    // Embedded hosts without Fullscreen API keep the viewport-filling workspace.
    if (!document.fullscreenElement && document.fullscreenEnabled && document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().catch(() => {});
    }
  }
  function exitFullscreen() {
    immersive(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }
  function openModel() {
    $("model-body").hidden = false;
    $("toggle-model").setAttribute("aria-expanded", "true"); text("toggle-model", "−");
    if (!$("model-dialog").open) $("model-dialog").showModal();
  }
  function previewStarting() {
    completedVersion = null; $("completion-banner").hidden = true;
    $("model-dialog").close(); $("results-dialog").close();
    sync();
  }
  function beginPreview() { previewStarting(); enterFullscreen(); }
  function previewLoaded() {
    completedVersion = null; $("completion-banner").hidden = true; sync();
  }
  function frame(current) {
    text("live-completed", `${format(current?.completed_total || 0, 0)} 件`);
    const machines = current?.machines || [];
    text("live-machines", `${machines.filter((machine) => machine.state === "processing").length} / ${activeVersion()?.config.machines.length || 0} 台`);
    text("live-buffer", `${(current?.buffers || []).reduce((total, buffer) => total + buffer.level, 0)} 件`);
    if (completedVersion && completedVersion !== key()) {
      completedVersion = null; $("completion-banner").hidden = true;
    }
  }
  function sync() {
    const version = activeVersion();
    const disabled = !version || state.busy || state.adjusting;
    $("edit-model").disabled = disabled; $("nav-model").disabled = disabled;
    $("nav-results").disabled = !version;
    // Keep transport controls available during playback; an in-flight AI change
    // locks the old replay until its outcome is known.
    for (const id of ["play-pause", "restart-playback", "seek"]) $(id).disabled = !state.frames.length || state.adjusting || hasPending("preview");
    text("workspace-model-summary", version
      ? `${version.config.machines.length} 台设备 · ${version.config.buffers.length} 个缓冲区 · ${durationLabel(version.config.until_seconds)} · ${version.config.replications} 次重复`
      : "正在载入输入模型…");
    text("workflow-status", state.adjusting ? "已暂停 · AI 正在创建新方案"
      : hasPending("preview") ? "正在计算预览 · 就绪后自动播放"
      : state.dirty ? "模型有修改 · 应用后重新运行"
      : state.playing ? "运行预览 · 可以随时暂停或发送调整指令"
      : completedVersion === key() && version ? "回放已完成 · 查看最终方案与评估"
      : state.frames.length ? "预览已就绪 · 点击播放继续观察"
      : "先检查输入模型，再运行预览");
    text("assistant-flow-message", state.adjusting
      ? "回放已暂停，正在校验 AI 修改。新方案将从初始状态运行。"
      : "输入和切换模型不影响回放；发送指令时暂停。" );
    $("assistant-flow-message").classList.toggle("working", state.adjusting);
    document.body.dataset.playing = String(state.playing);
    document.body.dataset.adjusting = String(state.adjusting);
  }

  function comparison(current, candidate) {
    const fields = ["until_seconds", "warmup_seconds", "replications", "base_seed", "confidence_level"];
    return fields.every((field) => current.config[field] === candidate.config[field]);
  }
  function resultVersion() {
    if (resultSelection && resultSelection.sessionId === state.session?.id) {
      const selected = state.session.versions.find((version) => version.id === resultSelection.versionId);
      if (selected) return selected;
    }
    resultSelection = null;
    return activeVersion();
  }
  function studyPending(version) {
    return state.startingRuns.has(`${state.session.id}/${version.id}/study`) || state.session.runs.some((run) =>
      run.version_id === version.id && run.kind === "study" && ["queued", "running"].includes(run.status));
  }
  function evaluation(version, study) {
    const paragraphs = [
      `查看方案：${versionName(version)}。${version.config.machines.length} 台串联设备、${version.config.buffers.length} 个中间缓冲区；${version.mode === "paper" ? "遵守论文案例一约束" : "使用自定义实验约束"}。`,
    ];
    if (version.note) paragraphs.push(`方案说明：${version.note}`);
    const changes = version.changes || [];
    paragraphs.push(changes.length ? `本次修改（${changes.length} 项）：${changes.map((change) => changeDescription(change, version.config)).join("；")}。` : "本方案为初始输入模型，尚无参数修改记录。");
    if (!study) {
      paragraphs.push("此版本尚无可读取的完整评估结果。单次采样预览指标不代表完整实验的最终结果。");
      if (version.id !== state.session.active_version_id) paragraphs.push("如需评估此历史模型，可在下方版本表中将它恢复为新方案，再运行完整评估。原版本记录会保留。");
      return paragraphs;
    }
    paragraphs.push(`评估口径：${durationLabel(version.config.until_seconds)}（含 ${durationLabel(version.config.warmup_seconds)}预热），${version.config.replications} 次独立重复；下列比较来自完整评估均值。`);
    const currentIndex = state.session.versions.findIndex((item) => item.id === version.id);
    const prior = state.session.versions.slice(0, currentIndex).find((item) => studyForVersion(item.id) && comparison(version, item));
    if (prior) {
      const control = studyForVersion(prior.id);
      paragraphs.push(`对照方案：${versionName(prior)}（评估时长、预热、重复次数、种子和置信水平相同）。`);
      for (const metric of metrics) {
        const current = study.summary?.find((row) => row.metric === metric.key)?.mean;
        const baseline = control.summary?.find((row) => row.metric === metric.key)?.mean;
        if (!Number.isFinite(current) || !Number.isFinite(baseline)) continue;
        const difference = current - baseline;
        const trend = Math.abs(difference) < 1e-9 ? "持平" : difference > 0 ? "增加" : "减少";
        const percent = baseline === 0 ? "（对照值为零，不计算百分比）" : `（${format(Math.abs(difference / baseline) * 100)}%）`;
        paragraphs.push(`${metric.label}：${format(baseline)} → ${format(current)} ${metric.unit}，${trend} ${format(Math.abs(difference))}${percent}。`);
      }
      paragraphs.push("这些变化是观测均值的描述，不是显著性检验或最优方案证明。请结合置信区间、产出目标与能耗约束选择方案。");
    } else {
      paragraphs.push("尚无评估条件相同的历史对照结果，暂不判定改进幅度。可评估其他版本，再在方案表中比较；不能仅凭一次预览认定方案更优。");
    }
    paragraphs.push("方案与评估说明由已保存的参数及仿真结果生成；此步骤不额外调用 AI。");
    return paragraphs;
  }
  function renderResults() {
    const version = resultVersion(); if (!version) return;
    const historical = version.id !== state.session.active_version_id;
    const selector = $("result-version-select");
    const options = [...state.session.versions].reverse().map((item) => ({
      id: item.id, label: `${versionName(item)}${item.id === state.session.active_version_id ? " · 当前方案" : ""}`,
    }));
    const revision = JSON.stringify(options);
    if (selector.dataset.revision !== revision) {
      selector.replaceChildren(...options.map((item) => {
        const option = node("option", "", item.label); option.value = item.id; return option;
      }));
      selector.dataset.revision = revision;
    }
    selector.value = version.id;
    $("results-dialog").dataset.versionId = version.id;
    $("return-current-results").hidden = !historical;
    $("run-study").hidden = historical;
    text("result-view-note", historical ? "正在查看历史方案；当前模型与仿真回放保持不变。" : "正在查看当前方案的运行结果。可切换查看历史版本。");
    const study = studyForVersion(version.id);
    const pending = studyPending(version);
    const latest = [...state.session.runs].reverse().find((run) => run.kind === "study" && run.version_id === version.id);
    text("result-status", pending ? `正在计算完整评估 · 完成后自动更新${study ? "；下方暂显示上一次成功结果" : ""}`
      : latest?.status === "failed" ? `最近评估失败${study ? " · 下方保留上一次成功结果" : ""}${historical ? " · 可恢复为新方案后重试" : " · 可点击评估当前方案重试"}`
      : study ? "完整评估已完成" : latest?.status === "succeeded" ? "此版本的评估结果暂时无法读取" : "此版本尚未完成完整评估");
    $("download-report").disabled = !study;
    $("result-cards").replaceChildren();
    for (const metric of metrics) {
      const row = study?.summary?.find((item) => item.metric === metric.key);
      const card = node("article", "result-card");
      card.append(node("span", "result-label", metric.label), node("strong", "mono", format(row?.mean)), node("span", "metric-unit", metric.unit));
      card.append(node("small", "ci", row
        ? row.ci_low == null || row.ci_high == null ? `n=${row.n} · 无法估计置信区间`
          : `${format((row.confidence_level || version.config.confidence_level) * 100, 0)}% CI ${format(row.ci_low)} – ${format(row.ci_high)}`
        : "等待完整评估"));
      $("result-cards").append(card);
    }
    reportText = evaluation(version, study);
    $("result-evaluation").replaceChildren(node("h3", "", "方案说明与结果解读"), ...reportText.map((paragraph) => node("p", "", paragraph)));
    const metadata = $("result-version-details");
    // Keep disclosure state and reading position when background jobs refresh.
    const detailKey = `${state.session.id}/${version.id}`;
    if (metadata.dataset.versionKey !== detailKey) {
      const json = node("details");
      json.append(node("summary", "text-button", "查看完整模型 JSON"), node("pre", "", JSON.stringify(version.config, null, 2)));
      metadata.replaceChildren(node("p", "small muted", `版本创建时间：${new Date(version.created_at).toLocaleString("zh-CN")}`));
      if (version.ai_config) {
        const source = node("details");
        source.append(node("summary", "text-button", `使用的 AI 模型：${aiSourceLabel(version.ai_config)}`), node("pre", "", JSON.stringify(version.ai_config, null, 2)));
        metadata.append(source);
      }
      metadata.append(json); metadata.dataset.versionKey = detailKey;
    }
    $("study-summary").hidden = !study;
    if (study) {
      const config = study.config || version.config;
      text("study-summary", `${versionName(version)} 已完成 ${config.replications} 次独立重复 · ${durationLabel(config.until_seconds)}（含 ${durationLabel(config.warmup_seconds)}预热）· ${format(config.confidence_level * 100, 0)}% 正态近似置信区间。比较不同版本时，请同时核对时长、预热和随机种子。`);
    }
    if (completedVersion === key()) {
      const activeStudy = studyForVersion(state.session.active_version_id);
      const banner = $("completion-banner"); banner.hidden = false;
      const status = banner.querySelector("[data-completion-status]");
      if (status) status.textContent = hasPending("study") ? "预览完成，正在生成完整方案评估…" : activeStudy ? "运行完成 · 最终指标与方案评估已就绪" : "预览完成 · 完整评估未完成，可在方案与评估中重试";
    }
  }
  async function complete() {
    if (!state.session || state.adjusting) return;
    completedVersion = key(); $("completion-banner").hidden = false;
    sync(); renderResults();
    // Playback completion orchestrates the existing study endpoint once per
    // immutable version. Manual evaluation and recovery use the same run cache.
    const requestKey = key();
    if (studyForVersion(state.session.active_version_id) || hasPending("study") || requestedStudies.has(requestKey)) return;
    requestedStudies.add(requestKey);
    try { await startRun("study", true); renderVersions(); }
    catch (error) {
      requestedStudies.delete(requestKey);
      if (requestKey === key()) { showError("global-error", error); renderResults(); }
    }
  }
  function openResults(versionId = null) {
    resultSelection = versionId ? {sessionId: state.session?.id, versionId} : null;
    renderResults();
    const dialog = $("results-dialog");
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
    $("results-title").focus({preventScroll: true});
  }
  const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
  function downloadReport() {
    const version = resultVersion(); const study = studyForVersion(version?.id);
    if (!study) return;
    renderResults();
    const rows = metrics.map((metric) => {
      const row = study.summary.find((item) => item.metric === metric.key);
      return `<tr><th>${escapeHTML(metric.label)}</th><td>${format(row?.mean)}</td><td>${escapeHTML(metric.unit)}</td><td>${row?.ci_low == null || row?.ci_high == null ? "无法估计" : `${format(row.ci_low)} – ${format(row.ci_high)}`}</td></tr>`;
    }).join("");
    const report = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SimPy Lab Studio · 最终方案报告</title><style>body{font:16px/1.8 system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 24px;color:#183b36}h1{font-size:28px}table{border-collapse:collapse;width:100%}th,td{padding:12px;border:1px solid #cdd8d2;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6f4;padding:20px}small{color:#52685e}</style><h1>最终方案与运行评估</h1><p>${escapeHTML(versionName(version))}</p><small>SimPy Lab Studio · ${escapeHTML(new Date().toLocaleString("zh-CN"))} · 完整重复实验结果</small><table><thead><tr><th>指标</th><th>均值</th><th>单位</th><th>${format(version.config.confidence_level * 100, 0)}% 正态近似置信区间</th></tr></thead><tbody>${rows}</tbody></table>${reportText.map((paragraph) => `<p>${escapeHTML(paragraph)}</p>`).join("")}<h2>最终输入模型</h2><pre>${escapeHTML(JSON.stringify(version.config, null, 2))}</pre><p>完整版本历史、每次重复的结果和采样帧可通过应用的“导出实验”保存为 ZIP。</p></html>`;
    downloadBlob(new Blob([report], {type: "text/html;charset=utf-8"}), `SimPy-Lab-Studio-report-${version.id.slice(0, 8)}.html`);
  }

  window.StudioWorkspace = {beginPreview, previewStarting, previewLoaded, openModel, openResults, frame, sync, renderResults, complete};
  const assistantPanel = document.querySelector(".assistant-panel");
  const assistantDialog = $("assistant-dialog");
  const assistantHome = document.createComment("running assistant dock position");
  assistantPanel.before(assistantHome);
  function keepConversationPosition(move) {
    const messages = $("messages");
    const top = messages.getBoundingClientRect().top;
    const atBottom = messages.scrollHeight - messages.clientHeight - messages.scrollTop < 24;
    const anchor = [...messages.children].find((item) => item.getBoundingClientRect().bottom > top);
    const rect = anchor?.getBoundingClientRect();
    const fraction = rect?.height ? Math.max(0, (top - rect.top) / rect.height) : 0;
    move();
    if (atBottom) messages.scrollTop = messages.scrollHeight;
    else if (anchor) {
      const after = anchor.getBoundingClientRect();
      messages.scrollTop += after.top - messages.getBoundingClientRect().top + fraction * after.height;
    }
  }
  $("expand-assistant").addEventListener("click", () => {
    if (assistantDialog.open) { assistantDialog.close(); return; }
    keepConversationPosition(() => {
      // Move the existing panel; inputs, selection, listeners and pending replies
      // stay attached to the same nodes in both views.
      assistantDialog.append(assistantPanel);
      text("expand-assistant", "↙ 收起");
      $("expand-assistant").setAttribute("aria-expanded", "true");
      $("expand-assistant").setAttribute("aria-label", "收起运行助手对话");
      $("expand-assistant").title = "收起并返回仿真主界面";
      assistantDialog.showModal();
    });
  });
  assistantDialog.addEventListener("close", () => {
    // The closed dialog is already hidden, so temporarily restore its layout
    // while measuring the conversation's reading position.
    assistantDialog.style.display = "block";
    keepConversationPosition(() => {
      assistantHome.after(assistantPanel);
      assistantDialog.style.removeProperty("display");
      text("expand-assistant", "⤢ 放大");
      $("expand-assistant").setAttribute("aria-expanded", "false");
      $("expand-assistant").setAttribute("aria-label", "放大运行助手对话");
      $("expand-assistant").title = "放大查看完整对话";
      $("expand-assistant").focus({preventScroll: true});
    });
  });
  for (const id of ["edit-model", "nav-model"]) $(id).addEventListener("click", openModel);
  $("close-model").addEventListener("click", () => $("model-dialog").close());
  for (const id of ["nav-results", "view-final-results", "return-current-results"]) $(id).addEventListener("click", () => openResults());
  $("result-version-select").addEventListener("change", (event) => {
    resultSelection = {sessionId: state.session.id, versionId: event.target.value};
    renderResults();
  });
  $("close-results").addEventListener("click", () => $("results-dialog").close());
  for (const id of ["model-dialog", "results-dialog", "assistant-dialog", "visual-model-dialog"]) {
    const dialog = $(id);
    let pressedOutside = false;
    const isOutside = (event) => {
      const rect = dialog.getBoundingClientRect();
      return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom);
    };
    // Require both ends of the click on the backdrop. Dragging from an input
    // or selecting text out of the panel must not dismiss it or discard a draft.
    dialog.addEventListener("pointerdown", (event) => {
      pressedOutside = event.isPrimary && event.button === 0 && isOutside(event);
    });
    dialog.addEventListener("pointercancel", () => { pressedOutside = false; });
    dialog.addEventListener("close", () => { pressedOutside = false; });
    dialog.addEventListener("click", (event) => {
      const dismiss = pressedOutside && isOutside(event);
      pressedOutside = false;
      if (dismiss) {
        event.preventDefault(); event.stopPropagation(); dialog.close();
      }
    });
  }
  $("nav-preview").addEventListener("click", () => { $("model-dialog").close(); $("results-dialog").close(); $("run-preview").focus(); });
  $("download-report").addEventListener("click", downloadReport);
  $("fullscreen-toggle").addEventListener("click", () => document.body.classList.contains("immersive") ? exitFullscreen() : enterFullscreen());
  $("workspace-exit").addEventListener("click", exitFullscreen);
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement) { wasFullscreen = true; immersive(true); }
    else if (wasFullscreen) { wasFullscreen = false; immersive(false); }
  });
  document.addEventListener("keydown", (event) => {
    const dialogOpen = document.querySelector("dialog[open]");
    if (event.key === "Escape" && !dialogOpen && document.body.classList.contains("immersive")) exitFullscreen();
    if (event.code !== "Space" || dialogOpen || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
    if (event.target.closest("input,textarea,select,button,a,summary,[contenteditable='true']")) return;
    if (!$("play-pause").disabled) { event.preventDefault(); state.playing ? pause() : play(); }
  });
  sync(); frame(state.frames[state.frameIndex]); renderResults();
})();
