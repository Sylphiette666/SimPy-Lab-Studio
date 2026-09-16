/* Synchronous editor cache with atomic persistence in the application data directory. */
"use strict";
(() => {
  const cache = new Map(), revisions = new Map(), pending = new Map(), running = new Set(), blocked = new Set();
  const failures = new Map();
  const sessionKey = "simlab.studio.session";
  const parse = key => {
    const match = /^simlab\.studio\.(input-draft|graph-draft|graph-layout)\.([a-f0-9-]{36})$/.exec(key);
    return match && {session: match[2], kind: {"input-draft": "input", "graph-draft": "graph", "graph-layout": "layout"}[match[1]]};
  };
  const keyFor = (id, kind) => `simlab.studio.${{input: "input-draft", graph: "graph-draft", layout: "graph-layout"}[kind]}.${id}`;
  function local(key, value) { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* Server storage still works. */ } }
  function getItem(key) {
    if (cache.has(key)) return cache.get(key);
    try { return localStorage.getItem(key); } catch { return null; }
  }
  async function flush(key) {
    if (running.has(key) || blocked.has(key) || !pending.has(key)) return;
    running.add(key);
    try {
      while (pending.has(key)) {
        const value = pending.get(key);
        const target = parse(key), body = JSON.stringify({value, revision: revisions.get(key) || 0});
        const response = await fetch(`/api/studio/sessions/${target.session}/drafts/${target.kind}`, {
          method: "PUT", headers: {"Content-Type": "application/json"}, body,
          keepalive: new TextEncoder().encode(body).length < 60000,
        });
        if (!response.ok) {
          await response.text();
          if (response.status === 409) { blocked.add(key); local(key + ".conflict", cache.get(key)); }
          throw new Error(response.status === 409 ? "另一窗口更新了草稿；冲突副本已保留，可从实验管理导出。" : "草稿尚未同步到本机文件，请保持窗口打开后重试。");
        }
        revisions.set(key, (await response.json()).revision);
        if (pending.get(key) === value) pending.delete(key);
        failures.delete(key);
      }
    } catch (error) {
      failures.set(key, blocked.has(key) ? error.message : "草稿尚未同步到本机文件，正在重试，请保持窗口打开。");
    } finally {
      running.delete(key);
      if (typeof updateControls === "function") updateControls();
    }
  }
  function setItem(key, value) {
    if (getItem(key) === value && (revisions.has(key) || pending.has(key))) return;
    cache.set(key, value); local(key, value);
    if (key === sessionKey) {
      fetch("/api/studio/workspace", {method: "PUT", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({last_session_id: value}), keepalive: true}).then(response => response.json()).catch(() => {});
    } else if (parse(key)) { pending.set(key, value); flush(key); }
  }
  async function prepare(id, supplied) {
    let data = supplied;
    if (!data) {
      const response = await fetch(`/api/studio/sessions/${id}/drafts`);
      if (response.status === 404) return;
      if (!response.ok) throw new Error("无法读取实验草稿，请稍后重试。");
      data = await response.json();
    }
    for (const kind of ["input", "graph", "layout"]) {
      const key = keyFor(id, kind), saved = data[kind];
      if (pending.has(key) || running.has(key) || blocked.has(key)) continue;
      if (saved) { revisions.set(key, saved.revision); cache.set(key, saved.value); local(key, saved.value); }
      else { const legacy = getItem(key); if (legacy) setItem(key, legacy); }
    }
  }
  async function initialize() {
    const response = await fetch("/api/studio/workspace");
    if (!response.ok) throw new Error("无法读取本机草稿存储。");
    const data = await response.json(), id = data.last_session_id || getItem(sessionKey);
    if (id) { cache.set(sessionKey, id); await prepare(id, data.last_session_id === id ? data.drafts : undefined); }
  }
  function conflicts() {
    const result = {};
    try { for (let i=0; i<localStorage.length; i++) { const key = localStorage.key(i); if (key?.endsWith(".conflict") && parse(key.slice(0, -9))) result[key] = JSON.parse(localStorage.getItem(key)); } } catch { /* Cache disabled. */ }
    for (const key of blocked) result[key] = JSON.parse(cache.get(key) || "null");
    return result;
  }
  window.StudioStorage = {getItem, setItem, removeItem: key => setItem(key, null), initialize, prepare, conflicts,
    statusMessage: () => failures.values().next().value || ""};
  setInterval(() => { for (const key of pending.keys()) flush(key); }, 3000);
})();
