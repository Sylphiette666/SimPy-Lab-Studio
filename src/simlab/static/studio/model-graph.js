/* Visual graph -> existing serial manufacturing config. No simulation logic. */
"use strict";
((root) => {
  const copy = value => JSON.parse(JSON.stringify(value));
  const machineFields = ["cycle_time_seconds", "availability", "mttr_seconds", "idle_power_kw", "processing_power_kw"];
  const bufferFields = ["capacity", "delay_seconds"];
  const kindName = type => ({machine: "设备", buffer: "容器", source: "原料", sink: "成品"})[type];
  function arrange(graph, order = graph.nodes.map(item => item.id)) {
    order.forEach((id, index) => {
      const item = graph.nodes.find(node => node.id === id);
      if (item) { item.x = 48 + (index % 4) * 220; item.y = 48 + Math.floor(index / 4) * 160; }
    });
  }
  function fromConfig(config) {
    const graph = {nodes: [{id: "source", type: "source", data: {name: "原料"}}], edges: [], nextId: 1};
    config.machines.forEach((machine, index) => {
      graph.nodes.push({id: `n${graph.nextId++}`, type: "machine", data: copy(machine)});
      if (config.buffers[index]) graph.nodes.push({id: `n${graph.nextId++}`, type: "buffer", data: copy(config.buffers[index])});
    });
    graph.nodes.push({id: "sink", type: "sink", data: {name: "成品"}});
    for (let index = 1; index < graph.nodes.length; index++) graph.edges.push({from: graph.nodes[index - 1].id, to: graph.nodes[index].id});
    arrange(graph); return graph;
  }
  const edgeId = edge => `${edge.from}:${edge.to}`;
  const canFollow = (from, to) => (from === "source" && to === "machine") ||
    (from === "machine" && ["buffer", "sink"].includes(to)) || (from === "buffer" && to === "machine");
  function connectionError(graph, from, to) {
    const a = graph.nodes.find(item => item.id === from), b = graph.nodes.find(item => item.id === to);
    if (!a || !b) return "连接端点已不存在。";
    if (from === to) return "不能连接到自身。";
    if (!canFollow(a.type, b.type)) return "连接顺序须为：原料 → 设备 → 容器 → 设备 → … → 成品。";
    if (graph.edges.some(edge => edge.from === from && edge.to === to)) return "这条连接线已存在。";
    if (graph.edges.some(edge => edge.from === from)) return "输出端已有连接，请先选中原连接线并删除。";
    if (graph.edges.some(edge => edge.to === to)) return "输入端已有连接，请先选中原连接线并删除。";
    const visited = new Set(), pending = [to];
    while (pending.length) {
      const id = pending.pop(); if (id === from) return "这条连接会形成环路，当前产线需要单向串联。";
      if (visited.has(id)) continue; visited.add(id);
      graph.edges.filter(edge => edge.from === id).forEach(edge => pending.push(edge.to));
    }
    return null;
  }
  function connect(graph, from, to) {
    const error = connectionError(graph, from, to); if (error) throw new Error(error);
    graph.edges.push({from, to});
  }
  function add(graph, type, x, y) {
    if (!["machine", "buffer"].includes(type)) throw new Error("请选择设备或容器。");
    const members = graph.nodes.filter(item => item.type === type), max = type === "machine" ? 12 : 11;
    if (members.length >= max) throw new Error(`最多支持 ${max} 个${kindName(type)}。`);
    let number = members.length + 1;
    while (members.some(item => item.data.name === `${kindName(type)}${number}`)) number++;
    const data = type === "machine" ? {name: `设备${number}`, cycle_time_seconds: 60, availability: 1,
      mttr_seconds: 0, idle_power_kw: 10, processing_power_kw: 50} : {name: `容器${number}`, capacity: 5, delay_seconds: 10};
    const item = {id: `n${graph.nextId++}`, type, data, x, y}; graph.nodes.push(item); return item.id;
  }
  function remove(graph, id) {
    const item = graph.nodes.find(node => node.id === id);
    if (!item || ["source", "sink"].includes(item.type)) throw new Error("原料和成品端点需要保留。");
    graph.nodes = graph.nodes.filter(node => node.id !== id);
    graph.edges = graph.edges.filter(edge => edge.from !== id && edge.to !== id);
  }
  function inspect(graph, base, mode, reference) {
    const issues = [], byId = new Map(graph.nodes.map(item => [item.id, item]));
    const fail = (message, nodeId, edge) => issues.push({message, nodeId, edgeId: edge && edgeId(edge)});
    const machines = graph.nodes.filter(item => item.type === "machine"), buffers = graph.nodes.filter(item => item.type === "buffer");
    if (machines.length < 1 || machines.length > 12) fail("请保留 1–12 台加工设备。");
    if (buffers.length !== machines.length - 1) fail(`当前 ${machines.length} 台设备需要 ${Math.max(0, machines.length - 1)} 个中间容器，已有 ${buffers.length} 个。`);
    for (const type of ["source", "sink"]) if (graph.nodes.filter(item => item.type === type).length !== 1) fail("需要且只能保留一个原料端和一个成品端。");
    if (byId.size !== graph.nodes.length) fail("图形包含重复节点标识，请重新载入输入模型。");
    const incoming = new Map(), outgoing = new Map(), seenEdges = new Set();
    for (const edge of graph.edges) {
      const from = byId.get(edge.from), to = byId.get(edge.to);
      if (!from || !to || !canFollow(from.type, to.type)) { fail("有一条连接线的端点或连接顺序不正确。", null, edge); continue; }
      if (seenEdges.has(edgeId(edge))) fail("存在重复连接线。", null, edge);
      seenEdges.add(edgeId(edge));
      if (outgoing.has(edge.from) || incoming.has(edge.to)) fail("每个端口只可连接一条线，请移除分支或汇合连接。", null, edge);
      outgoing.set(edge.from, edge.to); incoming.set(edge.to, edge.from);
    }
    for (const item of graph.nodes) {
      if (item.type !== "source" && !incoming.has(item.id)) fail(`“${item.data.name || kindName(item.type)}”缺少输入连接。`, item.id);
      if (item.type !== "sink" && !outgoing.has(item.id)) fail(`“${item.data.name || kindName(item.type)}”缺少输出连接。`, item.id);
    }
    const order = [], visited = new Set(); let id = graph.nodes.find(item => item.type === "source")?.id;
    while (id && !visited.has(id)) { order.push(id); visited.add(id); id = outgoing.get(id); }
    if (id) fail("连接中存在环路，请断开环路后重新连接。", id);
    if (visited.size !== graph.nodes.length) fail("所有设备和容器都必须连入从原料到成品的同一条产线。");
    const values = new Map();
    for (const [items, fields, type] of [[machines, machineFields, "machine"], [buffers, bufferFields, "buffer"]]) {
      const names = new Map();
      for (const item of items) {
        const data = {name: String(item.data.name || "").trim()};
        if (!data.name || data.name.length > 80) fail(`${kindName(type)}名称须为 1–80 个字符。`, item.id);
        if (names.has(data.name)) {
          fail(`${kindName(type)}名称“${data.name}”重复。`, names.get(data.name));
          fail(`${kindName(type)}名称“${data.name}”重复。`, item.id);
        }
        names.set(data.name, item.id);
        for (const field of fields) {
          const raw = item.data[field]; data[field] = Number(raw);
          if ((typeof raw !== "number" && typeof raw !== "string") || String(raw).trim() === "" || !Number.isFinite(data[field])) fail(`“${data.name}”的数值参数填写不完整。`, item.id);
        }
        if (type === "machine") {
          if (!(data.cycle_time_seconds >= 1)) fail(`“${data.name}”的加工时间至少为 1 秒。`, item.id);
          if (!(data.availability > 0 && data.availability <= 1)) fail(`“${data.name}”的可用率须大于 0% 且不超过 100%。`, item.id);
          if (!(data.mttr_seconds >= 0) || (data.availability < 1 && data.mttr_seconds <= 0)) fail(`“${data.name}”可用率小于 100% 时，维修时间必须大于 0。`, item.id);
          if (data.availability < 1 && data.availability * data.mttr_seconds / (1 - data.availability) < .1) fail(`“${data.name}”推算的平均故障间隔不能小于 0.1 秒。`, item.id);
          for (const field of ["idle_power_kw", "processing_power_kw"]) if (!(data[field] >= 0 && data[field] <= 1e9)) fail(`“${data.name}”的功率须在 0–1000000000 kW 之间。`, item.id);
        } else {
          if (!Number.isInteger(data.capacity) || data.capacity < 1 || data.capacity > 10000) fail(`“${data.name}”的容量须为 1–10000 的整数。`, item.id);
          if (!(data.delay_seconds >= 0)) fail(`“${data.name}”的转运时间不能为负数。`, item.id);
        }
        values.set(item.id, data);
      }
    }
    if (issues.length) return {ok: false, issues};
    const config = copy(base);
    config.machines = order.filter(id => byId.get(id).type === "machine").map(id => values.get(id));
    config.buffers = order.filter(id => byId.get(id).type === "buffer").map(id => values.get(id));
    const paper = reference && JSON.stringify(config.machines.map(item => item.name)) === JSON.stringify(reference.machines.map(item => item.name)) &&
      JSON.stringify(config.buffers.map(item => [item.name, item.capacity])) === JSON.stringify(reference.buffers.map(item => [item.name, item.capacity]));
    return {ok: true, issues: [], config, order, mode: mode === "paper" && paper ? "paper" : "custom"};
  }
  const exports = {fromConfig, arrange, connectionError, connect, add, remove, inspect, edgeId};
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  else root.StudioModelGraph = exports;
})(globalThis);
