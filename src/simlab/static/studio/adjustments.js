/* Generate -> review -> explicitly apply. Late or cancelled replies never apply locally. */
"use strict";
(() => {
  let job = null, sessionId = null, timer = null, polling = false, error = "", mutating = false;
  const panel = node("div", "adjustment-controls"); panel.id = "adjustment-controls";
  const status = node("p", "small"); status.id = "adjustment-status"; status.setAttribute("role", "status");
  const review = node("button", "button", "查看修改预览"); review.id = "review-adjustment";
  const cancel = node("button", "button", "取消请求"); cancel.id = "cancel-adjustment";
  const retry = node("button", "button", "重试生成"); retry.id = "retry-adjustment";
  for (const button of [review, cancel, retry]) button.type = "button";
  panel.append(status, review, cancel, retry); $("prompt-form").before(panel);
  const dialog = node("dialog", "modal wide-modal"); dialog.id = "adjustment-dialog";
  dialog.setAttribute("aria-label", "AI 修改预览");
  const title = node("h2", "", "AI 修改预览"), description = node("p"); description.id = "adjustment-note";
  const source = node("p", "small muted"), changes = node("ul"); changes.id = "adjustment-changes";
  const notice = node("p", "small", "预期影响是模型提出的假设。应用后从初始状态重新仿真，效果须用完整重复实验验证。");
  const failure = node("p", "notice error"); failure.id = "adjustment-error"; failure.hidden = true;
  const actions = node("div", "modal-actions");
  const close = node("button", "button", "稍后查看"), discard = node("button", "button", "放弃方案"), apply = node("button", "button primary", "确认应用并预览");
  apply.id = "apply-adjustment"; discard.id = "discard-adjustment";
  actions.append(close, discard, apply); dialog.append(title, source, description, changes, notice, failure, actions); document.body.append(dialog);
  const active = () => job && ["queued", "running", "ready"].includes(job.status);
  const path = suffix => `/sessions/${sessionId}/adjustments${suffix}`;
  const call = (route, options = {}) => api(route, {...options, signal: AbortSignal.timeout(15000)});
  function render() {
    panel.hidden = !job; if (!job) return;
    $("adjust-progress").hidden = !["queued", "running"].includes(job.status);
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(job.created_at)) / 1000));
    const names = {queued: "正在排队", running: "正在生成并校验方案", ready: "方案已就绪，等待确认", cancelled: "已取消，迟到的结果不会应用", failed: "生成失败", no_changes: "本轮没有参数修改", applied: "方案已应用"};
    status.textContent = error || job.error || `${names[job.status] || job.status}${["queued", "running"].includes(job.status) ? ` · ${seconds} 秒` : ""}`;
    review.hidden = job.status !== "ready";
    cancel.hidden = !active(); cancel.textContent = job.status === "ready" ? "放弃方案" : "取消请求";
    retry.hidden = !["failed", "cancelled"].includes(job.status);
    for (const button of [cancel, retry, apply, discard]) button.disabled = mutating;
    description.textContent = job.note || "";
    source.textContent = `${job.ai_config?.profile_name || ""} · ${job.ai_config?.model || ""}`;
    const base = state.session.versions.find(version => version.id === job.expected_version_id) || activeVersion();
    changes.replaceChildren(...(job.changes || []).map(change => node("li", "", changeDescription(change, base.config))));
    apply.disabled = mutating || job.status !== "ready" || job.expected_version_id !== state.session.active_version_id;
    const staleMessage = job.status === "ready" && job.expected_version_id !== state.session.active_version_id ? "当前模型已变化，请放弃此方案后重新生成。" : "";
    failure.hidden = !error && !staleMessage; failure.textContent = error || staleMessage;
    if (job.status === "no_changes") status.textContent += `：${job.note || ""}`;
  }
  function accept(next) {
    if (job?.id === next.id && ["cancelled", "failed", "applied"].includes(job.status) && ["queued", "running", "ready"].includes(next.status)) return;
    const before = job?.status; job = next; error = "";
    state.adjustmentStatus = job.status;
    const list = state.session.adjustments ||= [], index = list.findIndex(item => item.id === job.id);
    if (index < 0) list.push(job); else list[index] = job;
    if (job.status === "no_changes" && !state.session.messages.some(message => message.request_id === job.id)) {
      state.session.messages.push({role:"user", content:job.prompt, request_id:job.id},
        {role:"assistant", content:job.note, applied:false, ai_config:job.ai_config, request_id:job.id});
      renderMessages();
    }
    state.adjusting = Boolean(active()); state.requestAI = active() ? job.ai_config : null;
    renderAI(); render();
    if (job.status === "ready" && before !== "ready" && !dialog.open) dialog.showModal();
    if (!active() && dialog.open) dialog.close();
  }
  function schedule() {
    clearTimeout(timer);
    if (job && ["queued", "running"].includes(job.status)) timer = setTimeout(poll, 600);
  }
  async function poll() {
    if (polling || !job) return;
    const id = job.id, owner = sessionId; polling = true;
    try {
      const next = await call(path(`/${id}`));
      if (owner === sessionId && id === job?.id) accept(next);
    } catch { error = "暂时无法读取状态，正在重连；可尝试取消。"; render(); }
    finally { polling = false; schedule(); }
  }
  async function begin(prompt = $("prompt").value.trim()) {
    if (!prompt) return;
    if (!state.ai?.available) { await openSettings(); return; }
    if (state.dirty || window.StudioVisualEditor?.hasDraft()) { toast("请先应用或处理模型及图形草稿，再生成 AI 方案。"); return; }
    if (active() || mutating) return;
    sessionId = state.session.id; pause(); state.adjusting = true; renderAI();
    const requestId = crypto.randomUUID();
    job = {id: requestId, prompt, status: "queued", created_at: new Date().toISOString(), expected_version_id: state.session.active_version_id, ai_config: clone(state.ai)};
    state.adjustmentStatus = "queued";
    mutating = true; render();
    try {
      accept(await call(path(""), {method: "POST", body: {prompt, request_id: requestId, expected_version_id: job.expected_version_id, ai_profile_id: state.ai.profile_id}}));
    } catch (e) {
      // Resolve an uncertain submission by its id before allowing another generation.
      try { accept(await call(path(`/${requestId}`))); }
      catch (lookup) {
        if (lookup.status === 404 || e.status) accept({...job, status: "failed", error: e.message});
        else { error = "提交状态暂未确认，正在重连；请勿重复发送。"; render(); }
      }
    }
    mutating = false; render(); schedule();
  }
  async function cancelJob() {
    if (mutating || !job) return;
    mutating = true; render();
    try {
      accept(await call(path(`/${job.id}/cancel`), {method: "POST"}));
      toast("已停止本地等待并禁止应用；服务商可能仍完成已发出的请求。");
    } catch (e) { error = e.message; render(); }
    finally { mutating = false; render(); schedule(); }
  }
  async function applyJob() {
    if (mutating || job?.status !== "ready") return;
    if (state.dirty || window.StudioVisualEditor?.hasDraft()) { error = "请先处理未应用的模型草稿。"; render(); return; }
    mutating = true; render();
    try {
      const session = await call(path(`/${job.id}/apply`), {method: "POST"});
      $("prompt").value = ""; state.pendingMessages = []; state.adjusting = false;
      acceptSession(session); dialog.close();
      await startRun("preview", true); toast("已应用确认的修改，正在预览新方案。");
    } catch (e) { error = e.message; render(); }
    finally { mutating = false; render(); }
  }
  function recover() {
    clearTimeout(timer); sessionId = state.session.id;
    job = state.session.adjustments?.at(-1) || null;
    state.adjustmentStatus = job?.status || null;
    state.adjusting = Boolean(active()); state.requestAI = active() ? job.ai_config : null;
    error = ""; render(); schedule();
  }
  review.addEventListener("click", () => { render(); dialog.showModal(); });
  close.addEventListener("click", () => dialog.close());
  cancel.addEventListener("click", cancelJob); discard.addEventListener("click", cancelJob);
  retry.addEventListener("click", () => begin(job.prompt)); apply.addEventListener("click", applyJob);
  window.StudioAdjustments = {begin, recover}; panel.hidden = true;
})();
