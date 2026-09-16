/* Preserve incomplete input values per experiment, without storing API credentials. */
"use strict";
(() => {
  let restoring = false;
  const key = () => "simlab.studio.input-draft." + state.session.id;
  const inputs = () => [...document.querySelectorAll("#model-form input[id], #model-mode, #model-name")];
  const banner = node("div", "notice"); banner.id = "draft-recovery"; banner.hidden = true;
  const message = node("p"), restore = node("button", "button", "恢复旧草稿"), discard = node("button", "button", "放弃旧草稿");
  banner.append(message, restore, discard); $("model-form").before(banner);
  let conflict = null;
  function save() {
    if (!state.session || restoring) return;
    try {
      // Do not replace a conflicting recovered draft until the user chooses.
      if (!conflict) {
        const data = {version: state.session.active_version_id, prompt: $("prompt").value, dirty: state.dirty};
        if (state.dirty) Object.assign(data, {config: state.draft, breaks: state.breaks,
          values: Object.fromEntries(inputs().map(input => [input.id, input.value]))});
        window.StudioStorage.setItem(key(), JSON.stringify(data));
      }
    } catch { text("save-status", "草稿暂不能保存到本机，请及时保存模型。"); }
  }
  function apply(saved) {
    restoring = true;
    try {
      if (saved.dirty && saved.config?.machines && saved.values) {
        renderEditor(saved.config, saved.values["model-mode"] || "custom");
        for (const input of inputs()) if (Object.hasOwn(saved.values, input.id)) input.value = saved.values[input.id];
        state.breaks = clone(saved.breaks || []); state.dirty = true; updateMode(); renderBreakSummary();
      }
      $("prompt").value = saved.prompt || "";
      updateControls();
    } finally { restoring = false; }
  }
  function recover() {
    conflict = null; banner.hidden = true;
    try {
      const saved = JSON.parse(window.StudioStorage.getItem(key()) || "null");
      $("prompt").value = "";
      if (!saved) return;
      if (saved.dirty && saved.version !== state.session.active_version_id) {
        conflict = saved; banner.hidden = false;
        message.textContent = "发现旧版本的未保存草稿。当前方案已变化，恢复前请核对修改。";
        $("prompt").value = saved.prompt || "";
      } else apply(saved);
    } catch { toast("草稿无法读取，已保留已保存模型。"); }
  }
  function clear() {
    if (!state.session) return;
    try { window.StudioStorage.removeItem(key()); } catch { /* Save reports storage errors. */ }
    conflict = null; banner.hidden = true;
  }
  restore.addEventListener("click", () => {
    if (!conflict || !confirm("用旧草稿替换当前输入表？已保存方案仍会保留。")) return;
    const saved = conflict; conflict = null; banner.hidden = true; apply(saved); save();
  });
  discard.addEventListener("click", () => { clear(); save(); });
  document.addEventListener("input", event => { if (event.target.closest("#model-form, #model-name, #prompt")) save(); });
  document.addEventListener("change", event => { if (event.target.closest("#model-form, #model-mode")) save(); });
  $("breaks-form").addEventListener("submit", () => queueMicrotask(save));
  window.addEventListener("beforeunload", save);
  window.StudioDrafts = {save, recover, clear};
})();
