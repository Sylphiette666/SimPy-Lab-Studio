# 运行助手：搜索、复制与导出

此功能自 v1.2.0 正式版提供，对应改进名单第 1 项。后续开发继续使用 `frontend/simulation-workspace`，已发布的 v1.0.0 与 v1.1.0 文件保持原样。

## 使用

- 点击助手中的“搜索”，输入关键词，在当前实验的用户消息、助手回答和错误提示中查找。匹配内容高亮，用 ↑ / ↓ 或 Shift+Enter / Enter 跳到上一条、下一条匹配消息；数量按消息计算。搜索公式时也可输入原始公式片段，例如 `N_{out}`。
- 搜索不会隐藏其他消息。点击 × 清空；搜索框中按 Esc 先清空关键词，再按 Esc 可关闭放大窗口。打开其他实验时清空搜索。
- 每条消息右上角“复制”复制完整原文；代码块的“复制代码”保留换行和缩进。剪贴板不可用时会提示手动复制。Windows 剪贴板可能把换行规范为 CRLF。
- 选择 Markdown、TXT 原文或 JSON，再点击“导出”。导出包含**当前实验的全部对话**，不受关键词、滚动位置影响，保留模型名称和应用状态。TXT 保留正文原文中的 Markdown/LaTeX 标记；JSON 适合后续处理。等待回复或失败时，当前显示的临时消息也会包含在导出中。
- 导出不包含尚未发送的输入、API 配置目录、密钥或密钥密文。正文按原文保存，用户自己写进消息的内容也会原样包含。
- 助手回答支持标题、列表、引用、表格、行内代码和带语言名称的代码块。较宽的表格、代码和公式可以单独横向滚动。
- 支持常见 LaTeX 公式：行内 `$...$`、`\(...\)`，独立公式 `$$...$$`、`\[...\]`。不能识别的公式显示原文。代码块和行内代码不会被当作公式执行或改写；用户输入与错误提示继续按原文显示。

放大与收起共享同一组工具、搜索条件和输入草稿。滚动阅读较早回答时，新回复不会强制拉到最底部，可点“↓ 最新”跳转。搜索、复制、导出、放大不调用 AI，也不暂停仿真。发送改进指令的原有行为保持不变。

## 离线与维护

界面组件及公式字体都随 EXE 和源码包提供，运行时不请求 CDN。渲染使用固定版本 Marked、DOMPurify 和 KaTeX，版本、下载地址、npm 归档 SHA-512 与各文件 SHA-256 记录在 `src/simlab/static/studio/vendor/manifest.json`。原许可在对应目录中，也会收录到软件“开源组件许可”页面。

参考上游文档：[Marked 的渲染与清理要求](https://marked.js.org/)、[DOMPurify 配置](https://github.com/cure53/DOMPurify)、[KaTeX 受信内容与公式限制](https://katex.org/docs/options.html)。原始 HTML 显示为文本；Markdown 结果经过允许列表清理，公式使用 `trust: false` 与展开限制。不自动加载回答里的远程图片。

`tools/vendor_conversation.py` 用于重新获取校验过的固定依赖，无需 npm 构建。修改版本时应同步校验值、许可证并运行浏览器检查。Python 包资源规则包含嵌套字体；PyInstaller 递归打包整个静态目录。

## 验证

启动 `tests/studio_browser_fixture.py`，将 `STUDIO_TEST_URL` 设为该隔离服务后运行：

```powershell
node tools/check_conversation_browser.cjs
node tools/check_workspace_browser.cjs
node tools/check_history_results_browser.cjs
```

对话检查包括搜索导航、公式与表格、代码和剪贴板、完整导出、阅读位置、窄窗口、跨实验重置以及恶意内容不执行、不产生远程请求。测试数据与截图保存在忽略提交的 `outputs/conversation-qa`，不调用付费模型。
