/* Serial-line editing through the existing validated configuration API. */
"use strict";
(() => {
  let openedVersion = null;
  let removal = null;
  const versionKey = () => `${state.session?.id}/${state.session?.active_version_id}`;
  const limit = () => state.bootstrap?.limits?.machines || 12;
  const unavailable = () => !state.session || state.busy || state.adjusting;
  function uniqueName(prefix, items) {
    const names = new Set(items.map((item) => item.name));
    let number = items.length + 1;
    while (names.has(`${prefix}${number}`)) number += 1;
    return `${prefix}${number}`;
  }
  function nameField(label, id, value) {
    const wrapper = node("label", "field topology-name", label);
    const input = node("input");
    input.id = id; input.value = value; input.maxLength = 80;
    input.required = true; input.autocomplete = "off"; input.dataset.paperLocked = "true";
    input.addEventListener("input", setDirty); wrapper.append(input);
    return wrapper;
  }
  function render(config) {
    config.machines.forEach((machine, index) => {
      const card = $("machine-fields").children[index];
      const field = nameField("设备名称", `machine-${index}-name`, machine.name);
      field.classList.add("topology-wide");
      field.querySelector("input").addEventListener("input", (event) => {
        card.querySelector("summary").children[1].textContent = event.target.value.trim() || "未命名设备";
      });
      card.querySelector(".machine-inputs").prepend(field);
      const controls = node("div", "topology-machine-actions");
      const remove = node("button", "text-button topology-remove", "移除设备与配套容器");
      remove.type = "button";
      remove.addEventListener("click", action(() => openRemoval(index), "model-error"));
      controls.append(remove); card.append(controls);
    });
    config.buffers.forEach((buffer, index) => {
      $("buffer-fields").children[index].firstElementChild.replaceWith(nameField(`B${index + 1} · 容器名称`, `buffer-${index}-name`, buffer.name));
    });
    sync();
  }
  function sync() {
    const count = state.draft?.machines.length || 0;
    $("add-equipment").disabled = unavailable() || count >= limit();
    $("add-equipment").title = count >= limit() ? `最多支持 ${limit()} 台设备` : "添加设备及一个配套缓冲容器";
    text("topology-summary", `${count} / ${limit()} 台设备 · ${state.draft?.buffers.length || 0} 个缓冲容器。新增后可继续编辑名称与参数。`);
    const paper = $("model-mode").value === "paper";
    document.querySelectorAll(".topology-remove").forEach((button) => {
      button.disabled = unavailable() || paper || count <= 1;
      button.title = paper ? "切换为自定义实验后可移除" : count <= 1 ? "至少保留一台设备" : "移除前会显示受影响的容器";
    });
    $("confirm-equipment").disabled = unavailable();
    $("confirm-remove-equipment").disabled = unavailable();
  }
  function readCurrent() {
    if (!$("model-form").reportValidity()) throw new Error("请先修正输入模型中的无效参数。");
    return readDraft();
  }
  function openAddition() {
    if (unavailable()) return;
    const config = readCurrent();
    if (config.machines.length >= limit()) throw new Error(`最多支持 ${limit()} 台设备。`);
    openedVersion = versionKey();
    const position = $("equipment-position"); position.replaceChildren();
    for (let index = 0; index <= config.machines.length; index += 1) {
      const option = node("option", "", index === 0 ? "产线起点 · 第一台设备之前"
        : `在 ${config.machines[index - 1].name} 之后${index === config.machines.length ? "（产线末端）" : ""}`);
      option.value = String(index); position.append(option);
    }
    position.value = String(config.machines.length);
    const reference = config.machines.at(-1);
    $("equipment-name").value = uniqueName("设备", config.machines);
    $("container-name").value = uniqueName("容器", config.buffers);
    $("equipment-cycle").value = reference.cycle_time_seconds;
    $("equipment-availability").value = reference.availability * 100;
    $("equipment-mttr").value = reference.mttr_seconds;
    $("equipment-idle").value = reference.idle_power_kw;
    $("equipment-processing").value = reference.processing_power_kw;
    $("container-capacity").value = "5"; $("container-delay").value = "10";
    showError("equipment-error", null); linkPreview(); $("equipment-dialog").showModal();
  }
  function linkPreview() {
    const position = Number($("equipment-position").value);
    const names = [...document.querySelectorAll('#machine-fields input[id$="-name"]')].map((input) => input.value.trim());
    const machine = $("equipment-name").value.trim() || "新设备";
    const container = $("container-name").value.trim() || "新容器";
    text("equipment-link-preview", position === names.length
      ? `新增连接：${names.at(-1)} → ${container} → ${machine} → 成品`
      : `新增连接：${machine} → ${container} → ${names[position]}；其余现有容器保留。`);
  }
  function acceptDraft(config, focusIndex) {
    renderEditor(config, "custom"); setDirty();
    const card = $("machine-fields").children[focusIndex];
    if (card) { card.open = true; card.querySelector("input")?.focus(); }
  }
  function addEquipment() {
    if (unavailable()) return;
    if (openedVersion !== versionKey()) throw new Error("当前方案已变化，请关闭此窗口后重新添加。");
    const config = readDraft();
    if (config.machines.length >= limit()) throw new Error(`最多支持 ${limit()} 台设备。`);
    const position = Number($("equipment-position").value);
    if (!Number.isInteger(position) || position < 0 || position > config.machines.length) throw new Error("插入位置已失效，请重新打开添加窗口。");
    const value = (id) => Number($(id).value);
    const machine = {name: $("equipment-name").value.trim(), cycle_time_seconds: value("equipment-cycle"),
      availability: value("equipment-availability") / 100, mttr_seconds: value("equipment-mttr"),
      idle_power_kw: value("equipment-idle"), processing_power_kw: value("equipment-processing")};
    const buffer = {name: $("container-name").value.trim(), capacity: value("container-capacity"), delay_seconds: value("container-delay")};
    if (!machine.name || !buffer.name) throw new Error("设备和容器名称不能只包含空格。");
    if (config.machines.some((item) => item.name === machine.name)) throw new Error("设备名称已存在，请使用不同名称。");
    if (config.buffers.some((item) => item.name === buffer.name)) throw new Error("容器名称已存在，请使用不同名称。");
    if (machine.availability < 1 && machine.mttr_seconds <= 0) throw new Error("可用率小于 100% 时，平均维修时间必须大于 0。");
    // Existing edges retain their buffers; add one edge next to the new station.
    config.buffers.splice(Math.min(position, config.buffers.length), 0, buffer);
    config.machines.splice(position, 0, machine);
    $("equipment-dialog").close(); acceptDraft(config, position);
    toast("已添加到自定义输入模型。点击“应用修改并预览”保存并运行。");
  }
  function openRemoval(index) {
    if (unavailable() || $("model-mode").value === "paper") return;
    const config = readCurrent();
    if (config.machines.length <= 1) throw new Error("产线至少需要一台设备。");
    const bufferIndex = Math.min(index, config.buffers.length - 1);
    removal = {index, bufferIndex, key: versionKey(), machineName: config.machines[index].name};
    text("remove-equipment-description", `将从当前输入草稿移除“${config.machines[index].name}”及容器“${config.buffers[bufferIndex].name}”，并连接相邻工位。其他参数保留，已有方案和运行结果不变。`);
    showError("remove-equipment-error", null); $("remove-equipment-dialog").showModal();
  }
  function removeEquipment() {
    if (unavailable()) return;
    if (!removal || removal.key !== versionKey() || $("model-mode").value !== "custom") throw new Error("输入模型已变化，请重新选择要移除的设备。");
    const config = readDraft();
    if (config.machines.length <= 1 || config.machines[removal.index]?.name !== removal.machineName) throw new Error("设备已变化或已是最后一台设备。");
    config.machines.splice(removal.index, 1); config.buffers.splice(removal.bufferIndex, 1);
    $("remove-equipment-dialog").close(); acceptDraft(config, Math.max(0, removal.index - 1));
    toast("已从输入草稿移除；应用修改后才会保存为新方案。");
  }
  window.StudioTopology = {render, sync};
  bind("add-equipment", "click", openAddition, "model-error");
  bind("equipment-form", "submit", addEquipment, "equipment-error");
  bind("confirm-remove-equipment", "click", removeEquipment, "remove-equipment-error");
  for (const id of ["equipment-position", "equipment-name", "container-name"]) $(id).addEventListener("input", linkPreview);
  if (state.draft) { render(state.draft); updateControls(); }
  else sync();
})();
