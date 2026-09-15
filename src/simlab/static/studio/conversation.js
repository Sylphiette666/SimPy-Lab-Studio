/* Local conversation presentation only. Never sends prompts or changes simulation state. */
"use strict";
(() => {
  const el = (tag, className = "", text) => {
    const item = document.createElement(tag); item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const button = (id, label, title = label) => {
    const item = el("button", "button quiet", label); item.type = "button";
    if (id) item.id = id;
    item.dataset.label = label; item.title = title; item.setAttribute("aria-label", title); return item;
  };
  const container = document.getElementById("messages");
  const empty = [...container.childNodes].map(child => child.cloneNode(true));
  const toolbar = el("div", "chat-toolbar");
  const searchToggle = button("chat-search-toggle", "搜索", "搜索当前实验的对话");
  const latest = button("chat-latest", "↓ 最新", "跳到最新消息"); latest.hidden = true;
  const exportFormat = el("select", "chat-export-format"); exportFormat.id = "chat-export-format";
  exportFormat.setAttribute("aria-label", "对话导出格式");
  for (const [value, label] of [["md", "Markdown"], ["txt", "TXT 原文"], ["json", "JSON"]]) {
    const option = el("option", "", label); option.value = value; exportFormat.append(option);
  }
  const exportButton = button("chat-export", "导出", "导出当前实验的全部对话（不受搜索影响）");
  exportButton.disabled = true;
  toolbar.append(searchToggle, latest, exportFormat, exportButton);
  const searchBar = el("div", "chat-search-bar"); searchBar.id = "chat-search-bar"; searchBar.hidden = true;
  const search = el("input", "chat-search"); search.id = "chat-search"; search.type = "search";
  search.placeholder = "搜索对话内容…"; search.maxLength = 200;
  search.setAttribute("aria-label", "搜索对话内容"); search.setAttribute("aria-controls", "messages");
  const result = el("span", "chat-search-count", "输入关键词"); result.id = "chat-search-count";
  result.setAttribute("role", "status"); result.setAttribute("aria-live", "polite");
  const previous = button("chat-search-prev", "↑", "上一条匹配对话（Shift + Enter）");
  const next = button("chat-search-next", "↓", "下一条匹配对话（Enter）");
  const clear = button("chat-search-clear", "×", "清空对话搜索");
  searchToggle.setAttribute("aria-expanded", "false"); searchToggle.setAttribute("aria-controls", searchBar.id);
  searchBar.append(search, result, previous, next, clear);
  const notice = el("span", "sr-only"); notice.id = "chat-notice"; notice.setAttribute("role", "status");
  toolbar.append(notice);
  container.before(toolbar, searchBar);
  let messages = [], sessionId = null, sessionName = "", signature = null;
  let matches = [], selected = -1, searchTimer;
  const escape = value => String(value).replace(/[&<>"']/g, char =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]);
  const roleLabel = message => message.role === "user" ? "你" : message.role === "error" ? "调整未应用" : "运行助手";
  const sourceLabel = message => [message.ai_config?.profile_name || message.ai_config?.name, message.ai_config?.model].filter(Boolean).join(" · ");
  const statusLabel = message => message.pending ? "等待回复" : message.applied === true ? "已应用" : message.applied === false ? "未修改模型" : "";

  // Tokenize math before Markdown consumes backslashes/underscores. Code tokens remain literal.
  let formulas = [];
  const mathExtension = (name, level, pattern, startPattern) => ({
    name, level,
    start(src) { const index = src.search(startPattern); return index < 0 ? undefined : index; },
    tokenizer(src) {
      const match = pattern.exec(src);
      if (match) return {type: name, raw: match[0], expression: match[1] ?? match[2], display: level === "block" || match[0].startsWith("$$")};
    },
    renderer(token) {
      const index = formulas.push(token) - 1;
      const tag = token.display ? "div" : "span";
      return `<${tag} class="chat-math" data-math-index="${index}">${escape(token.raw)}</${tag}>`;
    },
  });
  const parser = window.marked && new window.marked.Marked({gfm: true, breaks: true,
    renderer: {
      html(token) { return escape(token.text); },
      image(token) { return escape(`[图片：${token.text || "图片"}]`); },
    },
    extensions: [
      mathExtension("chatBlockMath", "block", /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])(?:\n|$)/, /\$\$|\\\[/),
      mathExtension("chatInlineMath", "inline", /^(?:\\\(([^\n]+?)\\\)|\$(?!\s|\$)((?:\\.|[^$\n\\])+?)(?<!\s)\$(?!\d))/, /\\\(|\$(?!\s|\$)/),
    ],
  });
  function richContent(raw) {
    const body = el("div", "message-content rich-message");
    if (!parser || !window.DOMPurify) { body.textContent = raw; body.classList.add("plain-message"); return body; }
    try {
      formulas = [];
      body.append(window.DOMPurify.sanitize(parser.parse(raw), {
        RETURN_DOM_FRAGMENT: true,
        ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "a", "span", "div"],
        ALLOWED_ATTR: ["href", "title", "class", "start", "align", "data-math-index"],
        ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false,
      }));
      for (const link of body.querySelectorAll("a")) {
        // No relative/application links; navigation requires an explicit external web link.
        if (!/^https?:\/\//i.test(link.getAttribute("href") || "")) link.removeAttribute("href");
        else { link.target = "_blank"; link.rel = "noopener noreferrer"; }
      }
      for (const math of body.querySelectorAll(".chat-math")) {
        const token = formulas[Number(math.dataset.mathIndex)];
        delete math.dataset.mathIndex;
        if (!token || !window.katex) continue;
        math.dataset.mathSource = token.expression;
        try {
          window.katex.render(token.expression, math, {displayMode: token.display, throwOnError: true,
            trust: false, strict: "ignore", maxExpand: 1000, maxSize: 20, output: "htmlAndMathml"});
        } catch {
          math.textContent = token.raw; math.classList.add("chat-math-fallback");
          math.title = "此公式暂无法排版，已保留原文。";
        }
      }
      for (const table of body.querySelectorAll("table")) {
        const wrapper = el("div", "chat-table-scroll"); wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region"); wrapper.setAttribute("aria-label", "回答中的表格，可横向滚动");
        table.before(wrapper); wrapper.append(table);
      }
      for (const pre of body.querySelectorAll("pre")) {
        const code = pre.querySelector("code"); if (!code) continue;
        const block = el("div", "chat-code-block"), heading = el("div", "chat-code-heading");
        const language = [...code.classList].find(name => name.startsWith("language-"))?.slice(9) || "代码";
        const copy = button("", "复制代码"); copy.classList.add("chat-copy-code");
        const original = code.textContent;
        copy.addEventListener("click", () => copyText(original, copy));
        heading.append(el("span", "", language), copy); pre.before(block); block.append(heading, pre);
        pre.tabIndex = 0; pre.setAttribute("aria-label", `${language} 代码，可横向滚动`);
      }
    } catch {
      body.textContent = raw; body.classList.add("plain-message");
    }
    return body;
  }
  async function copyText(value, trigger) {
    let copied = false;
    try { await navigator.clipboard.writeText(value); copied = true; } catch { /* WebView fallback below. */ }
    if (!copied) {
      const focused = document.activeElement, selection = window.getSelection();
      const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
      const temporary = el("textarea", "chat-clipboard"); temporary.value = value;
      document.querySelector(".assistant-panel").append(temporary); temporary.focus({preventScroll: true}); temporary.select();
      try { copied = document.execCommand("copy"); } catch { /* Report failure instead of claiming success. */ }
      temporary.remove(); focused?.focus({preventScroll: true});
      if (selection) { selection.removeAllRanges(); for (const range of ranges) selection.addRange(range); }
    }
    notice.textContent = copied ? "已复制原文。" : "复制失败，请选择文本后按 Ctrl + C。";
    const label = trigger.textContent; trigger.textContent = copied ? "已复制" : "请手动复制";
    clearTimeout(trigger.copyTimer); trigger.copyTimer = setTimeout(() => { trigger.textContent = trigger.dataset.label || label; }, 2000);
  }
  function atBottom() { return container.scrollHeight - container.clientHeight - container.scrollTop < 28; }
  function syncLatest() { latest.hidden = !messages.length || atBottom(); }
  function goTo(article) {
    const rect = article.getBoundingClientRect(), viewport = container.getBoundingClientRect();
    container.scrollTop += rect.top - viewport.top - 8;
  }
  function highlight(body, query) {
    // Join visible text so a match can cross Markdown emphasis boundaries. Never rewrite KaTeX.
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {acceptNode: item =>
      item.parentElement.closest(".chat-math, .chat-code-heading") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
    const nodes = []; let item, offset = 0;
    while ((item = walker.nextNode())) { nodes.push({item, offset}); offset += item.length; }
    const full = nodes.map(entry => entry.item.data).join("");
    // Literal, case-insensitive matching; do not treat user input as a regular expression.
    const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
    const ranges = [...full.matchAll(pattern)].map(match => [match.index, match.index + match[0].length]);
    for (const entry of nodes) {
      const overlaps = ranges.filter(([start, end]) => start < entry.offset + entry.item.length && end > entry.offset);
      if (!overlaps.length) continue;
      const fragment = document.createDocumentFragment(); let cursor = 0;
      for (const [start, end] of overlaps) {
        const left = Math.max(0, start - entry.offset), right = Math.min(entry.item.length, end - entry.offset);
        fragment.append(document.createTextNode(entry.item.data.slice(cursor, left)), el("mark", "chat-highlight", entry.item.data.slice(left, right)));
        cursor = right;
      }
      fragment.append(document.createTextNode(entry.item.data.slice(cursor))); entry.item.replaceWith(fragment);
    }
    for (const math of body.querySelectorAll(".chat-math")) {
      math.classList.toggle("chat-math-match", (math.dataset.mathSource || math.textContent).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    }
  }
  function selectMatch(index, scroll = true) {
    container.querySelector(".chat-search-current")?.classList.remove("chat-search-current");
    selected = matches.length ? (index + matches.length) % matches.length : -1;
    if (selected >= 0) { matches[selected].classList.add("chat-search-current"); if (scroll) goTo(matches[selected]); }
    result.textContent = !search.value.trim() ? "输入关键词" : matches.length ? `${selected + 1} / ${matches.length} 条` : "无匹配";
    previous.disabled = next.disabled = !matches.length;
    clear.disabled = !search.value;
  }
  function findMessages(scroll = true) {
    clearTimeout(searchTimer); searchTimer = null;
    const current = matches[selected]?.dataset.messageIndex;
    for (const mark of container.querySelectorAll("mark.chat-highlight")) mark.replaceWith(document.createTextNode(mark.textContent));
    container.normalize(); container.querySelectorAll(".chat-math-match").forEach(math => math.classList.remove("chat-math-match"));
    matches = []; const query = search.value.trim(), lower = query.toLocaleLowerCase();
    if (query) for (const article of container.querySelectorAll(".message")) {
      const message = messages[Number(article.dataset.messageIndex)], body = article.querySelector(".message-content");
      const haystack = `${message.content}\n${sourceLabel(message)}\n${body.textContent}`.toLocaleLowerCase();
      if (haystack.includes(lower)) { matches.push(article); highlight(body, query); }
    }
    const index = matches.findIndex(article => article.dataset.messageIndex === current);
    selectMatch(Math.max(0, index), scroll);
  }
  function render(incoming, session) {
    const changedSession = sessionId !== (session?.id || null);
    sessionId = session?.id || null; sessionName = session?.versions?.[0]?.config?.name || "仿真实验";
    const nextSignature = JSON.stringify(incoming);
    if (!changedSession && signature === nextSignature) return;
    signature = nextSignature;
    const bottom = atBottom(), oldTop = container.scrollTop;
    const top = container.getBoundingClientRect().top;
    const anchor = [...container.children].find(child => child.getBoundingClientRect().bottom > top);
    const anchorIndex = anchor?.dataset.messageIndex, anchorOffset = anchor?.getBoundingClientRect().top - top;
    messages = incoming.map(message => ({...message})); container.replaceChildren();
    if (changedSession) { search.value = ""; matches = []; selected = -1; }
    if (!messages.length) container.append(...empty.map(child => child.cloneNode(true)));
    messages.forEach((message, index) => {
      const role = ["user", "error"].includes(message.role) ? message.role : "assistant";
      const article = el("article", `message ${role}${message.pending ? " pending" : ""}`);
      article.dataset.messageIndex = index;
      const meta = el("div", "message-meta"); meta.append(el("span", "", roleLabel(message)));
      const status = statusLabel(message); if (status) meta.append(el("span", "tiny-badge", status));
      const copy = button("", "复制", `复制第 ${index + 1} 条对话原文`); copy.classList.add("chat-copy-message"); copy.dataset.label = "复制";
      copy.addEventListener("click", () => copyText(String(message.content ?? ""), copy)); meta.append(copy); article.append(meta);
      if (message.ai_config && role !== "user") {
        const source = el("div", "message-model", sourceLabel(message));
        source.title = [message.ai_config.base_url, message.ai_config.api_format].filter(Boolean).join(" · "); article.append(source);
      }
      const content = String(message.content ?? "");
      article.append(role === "assistant" ? richContent(content) : el("div", "message-content plain-message", content));
      container.append(article);
    });
    exportButton.disabled = !messages.length;
    if (search.value.trim()) findMessages();
    else {
      selectMatch(0, false);
      if (changedSession || bottom || messages.at(-1)?.pending) container.scrollTop = container.scrollHeight;
      else {
        const restored = [...container.children].find(child => child.dataset.messageIndex === anchorIndex);
        container.scrollTop = oldTop;
        if (restored) container.scrollTop += restored.getBoundingClientRect().top - container.getBoundingClientRect().top - anchorOffset;
      }
    }
    syncLatest();
  }
  function exportConversation() {
    if (!messages.length) return;
    // Explicit allowlist: profiles, credentials, model configuration and draft are never exported here.
    const records = messages.map(message => ({role: message.role, content: String(message.content ?? ""),
      ...(message.created_at ? {created_at: message.created_at} : {}),
      ...(message.version_id ? {version_id: message.version_id} : {}),
      ...(typeof message.applied === "boolean" ? {applied: message.applied} : {}),
      ...(message.pending ? {pending: true} : {}),
      ...(sourceLabel(message) ? {model: sourceLabel(message)} : {})}));
    const date = new Date(), format = exportFormat.value;
    let output;
    if (format === "json") output = JSON.stringify({format: "simlab-conversation", format_version: 1,
      session_id: sessionId, session_name: sessionName, exported_at: date.toISOString(), messages: records}, null, 2);
    else {
      const markdown = format === "md";
      output = `${markdown ? "# " : ""}SimPy Lab Studio · 运行助手对话\n\n实验：${sessionName}\n实验 ID：${sessionId}\n导出时间：${date.toISOString()}\n共 ${messages.length} 条；正文保留原文。\n`;
      messages.forEach((message, index) => {
        const details = [message.created_at, sourceLabel(message), statusLabel(message)].filter(Boolean).join(" · ");
        output += `\n${markdown ? "## " : "—— "}${index + 1}. ${roleLabel(message)}\n${details ? details + "\n" : ""}\n${String(message.content ?? "")}\n`;
      });
    }
    const mime = format === "json" ? "application/json" : format === "md" ? "text/markdown" : "text/plain";
    downloadBlob(new Blob([format === "txt" ? "\uFEFF" : "", output], {type: `${mime};charset=utf-8`}),
      `SimPy-Lab-Studio-conversation-${(sessionId || "local").slice(0, 8)}-${date.toISOString().replace(/[:.]/g, "-")}.${format}`);
    notice.textContent = `已导出全部 ${messages.length} 条对话。`;
  }
  searchToggle.addEventListener("click", () => {
    searchBar.hidden = !searchBar.hidden; searchToggle.setAttribute("aria-expanded", String(!searchBar.hidden));
    if (!searchBar.hidden) search.focus();
    else { search.value = ""; findMessages(false); }
  });
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => findMessages(), 120); });
  search.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Enter") { event.preventDefault(); if (searchTimer) findMessages(false); selectMatch(selected + (event.shiftKey ? -1 : 1)); }
    if (event.key === "Escape" && search.value) { event.preventDefault(); event.stopPropagation(); search.value = ""; findMessages(false); }
  });
  previous.addEventListener("click", () => selectMatch(selected - 1));
  next.addEventListener("click", () => selectMatch(selected + 1));
  clear.addEventListener("click", () => { search.value = ""; findMessages(false); search.focus(); });
  latest.addEventListener("click", () => { search.value = ""; findMessages(false); container.scrollTop = container.scrollHeight; });
  container.addEventListener("scroll", syncLatest, {passive: true});
  new ResizeObserver(syncLatest).observe(container);
  exportButton.addEventListener("click", exportConversation);
  selectMatch(0, false);
  window.StudioConversation = {render};
})();
