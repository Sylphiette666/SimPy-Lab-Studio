# SimPy Lab Studio

[下载 v1.1.0 安装程序](https://github.com/Sylphiette666/SimPy-Lab-Studio/releases/download/v1.1.0/SimPy-Lab-Studio-Setup-1.1.0.exe) · [下载 v1.1.0 源码 ZIP](source-packages/SimPy-Lab-Studio-1.1.0-source.zip?raw=true) · [源码包说明与校验](source-packages/README.md)

> 当前 `main` 为 **v1.1.0 正式版**，由已验证的测试版合并发布，包含新版仿真工作台、设备与容器编辑、AI 响应修复和助手对话放大。第一版继续保留在 [v1.0.0 发布记录](https://github.com/Sylphiette666/SimPy-Lab-Studio/releases/tag/v1.0.0)，后续开发仍使用 `frontend/simulation-workspace`。

本分支为基于 v1.1.0 的滚动测试版，新增**历史方案完整评估详情**：在版本表点击“详情”，即可查看该版本的指标、置信区间、方案解读、修改记录、AI 来源与完整模型，并导出对应报告。窗口内支持切换版本和返回当前方案；只查看历史不会恢复模型或暂停回放。此更新尚未发布到正式版安装包。[下载当前测试版源码](source-packages/SimPy-Lab-Studio-test-source.zip?raw=true)。

测试版已完成第 1 项：**运行助手增强**。支持对话搜索和匹配跳转、逐条复制与复制代码，以及 Markdown / TXT / JSON 全部对话导出；表格、代码和 LaTeX 公式可离线排版显示。侧栏和放大窗口均可使用，阅读旧消息时保持位置。详见 [对话工具说明](docs/conversation-tools.md)。

本次完成第 6 项：**模型可视化编辑**。点击“图形建模”，拖入设备与容器、拖动端口建立连接，点击节点编辑参数；支持撤销/重做、删除连线和自动排列。连接完整后可应用到输入模型或直接运行预览，加工顺序按连线确定，保留原有串联仿真与后端校验。详见 [图形建模说明](docs/visual-model-editor.md)；编号对应关系见 [原改进名单](docs/improvement-list.md)。

带独立窗口的 Windows 制造仿真软件：编辑生产线模型，观看 SimPy 状态回放，随时切换 AI 模型，用自然语言生成新方案，并通过重复实验比较结果。

在“编辑模型 → 加工设备”中点击 **“添加设备与容器”**，可选择插入位置、填写设备参数和配套容器的容量/转运时间。添加后进入自定义实验，最多支持 12 台串联设备；原论文方案保留。编辑名称或移除工位后，点击“应用修改并预览”保存并运行。

![仿真工作台预览](docs/images/workspace-preview.png)

## 下载与启动

推荐分享 [v1.1.0 Release](https://github.com/Sylphiette666/SimPy-Lab-Studio/releases/tag/v1.1.0) 中的 **`SimPy-Lab-Studio-Setup-1.1.0.exe`**。双击进入中文安装向导，选择位置和快捷方式即可完成安装。安装程序会检测 WebView2，缺失时使用微软官方程序联网安装；电脑已有 WebView2 时可以离线安装。

已安装旧版的用户，先退出软件再运行新版安装程序，可覆盖更新并保留实验数据。第一版安装程序和便携包仍可从 v1.0.0 发布记录下载。版本变化见 [v1.1.0 发布说明](docs/releases/v1.1.0.md)。

安装完成后可从桌面或开始菜单启动，在 Windows“已安装的应用”中卸载。卸载保留实验数据。详见 [安装和分享说明](docs/installer.md)。

也可以选择便携版 ZIP，解压后双击：

```text
SimPy Lab Studio/
└── SimPy Lab Studio.exe
```

便携版文件夹中只有一个 EXE，图标和界面资源均已内置。安装版还会添加卸载程序和使用说明。两种版本均无需安装 Python、运行 CMD 或先启动服务器；后台服务随软件自动启动，主界面显示在独立桌面窗口中。退出软件会停止它启动的服务。

支持 Windows 10/11 x64，需要系统的 Microsoft Edge WebView2 Runtime；不要求打开 Edge 浏览器。缺失时从 [Microsoft 官方页面安装](https://developer.microsoft.com/microsoft-edge/webview2/)。首次打开单文件 EXE 需要解压内置运行库，请稍候。本开源发行版尚未进行商业代码签名。

## 可以做什么

- 从论文案例一的四台串联设备开始，修改加工时间、可用率、维修时间、功率、缓冲区和班次；支持 JSON 导入导出。
- 在占据主要区域的画布中观看真实 SimPy 状态的二维回放：设备加工、堵塞、缺料、故障、缓冲占用、产出和指标曲线；运行预览进入全屏工作区，可暂停、变速和拖动时间轴。
- 在“模型接入”中保存多套 API 地址、模型名称和接口格式，例如 OpenAI GPT、DeepSeek 或兼容服务；在助手面板中随时切换。
- 点击运行助手右上角“放大”，在大窗口中阅读完整对话和继续输入；放大后正文字号增大，可通过“收起”、外侧点击或 Esc 返回侧栏。切换视图不暂停回放，也不清空消息或未发送的输入。
- 输入调整目标时保持播放；发送有效指令后暂停回放，由所选模型提出结构化修改，通过参数和案例约束校验后创建新版本并从初始状态重新仿真。每次回复与版本记录实际使用的 AI 模型。
- 回放自然结束后，自动为当前版本请求完整重复实验；也可手动评估。比较产出率、平均在制品与单位能耗的均值和置信区间，查看方案说明、恢复历史方案，导出实验 ZIP 与独立 HTML 报告。

API 密钥默认仅本次运行使用。测试版在“模型接入”增加可选的**在本机加密保存密钥**：勾选并保存后，重启软件自动恢复；取消勾选并保存会移除磁盘副本，“移除此配置密钥”同时清除本次使用和已保存的密钥。使用 Windows 当前用户 DPAPI 保护，磁盘只写密文；密钥及密文均不回传给界面或加入实验导出包。没有 API 密钥也可以使用编辑、回放、评估和导出。详见 [密钥保存说明](docs/credential-storage.md)。

## 使用顺序

1. 打开应用，点击“编辑模型”或左侧“模型”，修改初始输入；所有机器、缓冲区、班次和实验设置都在输入模型窗口中。点击窗口外的遮罩即可返回主界面，再次打开时保留当前草稿；仍需点击“应用修改并预览”才会保存并运行。
2. 点击“应用修改并预览”，或关闭编辑窗口后点击“运行预览”，进入全屏工作区。中央观察生产线，右侧查看动态指标，底部可暂停、重播、拖动进度和变速。
3. 需要 AI 调整时，在“模型接入”填写服务提供商的 API Base URL、模型和密钥并启用；右侧可随时切换配置。
4. 在运行助手输入目标，例如“保持缓冲容量不变，将第一台设备的可用率提高至 90%”。打字和切换模型不会暂停；点击发送或按 Ctrl+Enter 后暂停，校验成功的新版本自动重新运行。请求失败时，原回放仍可手动继续。
5. 回放自然结束后自动计算完整评估，完成后点击“查看最终结果”；也可随时通过“评估”入口手动评估当前方案。查看最终指标、修改记录与条件一致的历史对照，导出结果报告或完整实验。

![最终方案与运行评估](docs/images/workspace-results.png)

完整评估就绪时显示提示，不会自动弹窗抢走焦点。全屏模式保留助手、指标和控制按钮；不支持浏览器 Fullscreen API 的桌面宿主使用占满应用视口的沉浸布局。

模型和评估窗口均支持点击窗口外关闭，也保留关闭按钮和 Esc。窗口内点击、滚动及从内部拖动到外部不会触发关闭；关闭评估窗口不影响正在计算的任务。

本机保留第一版及一个持续覆盖更新的工作版本，不再按功能另建软件目录。正式发布在 `main` 和带版本号的 Release 上，测试分支用于后续迭代。第一版文件、用户实验及模型连接列表不会因发布而删除，Git 历史可追溯各次修改。

软件“帮助”菜单内也有使用说明、软件信息和组件许可。完整说明见 [桌面版使用说明](docs/desktop.md) 与 [模型和仿真说明](docs/studio.md)。

## 数据与边界

数据保存在 `%LOCALAPPDATA%\SimPy Lab Studio`；可从“文件 → 打开实验数据目录”进入。软件旁不产生数据文件。替换 EXE 不会删除实验记录；关闭窗口后未完成的任务下次需要重新运行。

暂停控制的是已计算采样帧的回放，不会冻结后台正在计算的 SimPy 进程。每次 AI 调整会从初始状态运行新模型，不会把加工到一半的工件直接转移到新模型。预览可能只覆盖完整实验的一段时间；正式评估按所设完整时长与重复次数计算，结果单独显示。当前是串联制造线的二维离散事件仿真，不包含任意流程拖拽建模或写实三维工厂。工程应用需结合实际生产数据校准；详见 [案例一参数与假设](docs/paper_case_a.md)。

## 从源码运行与构建

需要 Windows、Python 3.11+（发行版使用 Python 3.13 x64）。

```powershell
git switch main
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[desktop,desktop-build,dev]"
.venv\Scripts\python.exe desktop_entry.py --data-dir outputs/workspace-desktop

# 可选：本地构建此分支；产物位于 dist/，请勿覆盖保存的第一版发行文件
.venv\Scripts\python.exe tools/build_desktop.py

# 在装有 Inno Setup 6.7+ 的 Windows 上构建安装程序
.\tools\build_installer.ps1 -Python .venv\Scripts\python.exe

# 单元与集成测试
.venv\Scripts\python.exe -m pytest
```

浏览器开发模式仍可使用 `python -m simlab.cli studio --open-browser`。桌面壳使用 [pywebview](https://pywebview.flowrl.com/guide/freezing)，单文件构建使用 [PyInstaller](https://pyinstaller.org/en/stable/usage.html)。构建会收集组件许可并嵌入软件，产物不包含用户实验或真实 API 密钥。

改版的浏览器交互验证使用 `tools/check_workspace_browser.cjs`，覆盖布局、播放与暂停、发送时调整、完整评估、导出及原功能入口；测试服务注入固定 AI 响应，不调用付费 API。启动方法见 [改版验证说明](docs/workspace-redesign.md#开发运行与验证)。

本仓库是 [SimPy KPI Lab](https://github.com/Sylphiette666/SimPy-Kpi-Lab) 的独立桌面发行项目，保留底层仿真与测试代码，按 MIT 许可发布。
