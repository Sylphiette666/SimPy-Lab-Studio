# SimPy Lab Studio

> 当前分支 `frontend/simulation-workspace` 为仿真工作台界面预览版。中央仿真画面、全屏预览、旁侧指标与运行助手、结束后的方案评估均使用原有仿真接口；核心 Python 实现保持不变。设计来源、交互边界与验证方法见 [工作台改版说明](docs/workspace-redesign.md)。

带独立窗口的 Windows 制造仿真软件：编辑生产线模型，观看 SimPy 状态回放，随时切换 AI 模型，用自然语言生成新方案，并通过重复实验比较结果。

![仿真工作台预览](docs/images/workspace-preview.png)

## 第一版下载与启动

**以下下载提供第一版 v1.0.0，并不包含本分支的界面改版。** 第一版主分支、EXE、安装程序和发布记录继续保留；本次改版仅发布在独立分支。体验改版请按下文从此分支源码启动。

分享第一版给其他人时，推荐从 [v1.0.0 Releases 下载安装程序](https://github.com/Sylphiette666/SimPy-Lab-Studio/releases/tag/v1.0.0)中的 **`SimPy-Lab-Studio-Setup-1.0.0.exe`**。双击进入中文安装向导，选择位置和快捷方式即可完成安装。安装程序会检测 WebView2，缺失时使用微软官方程序联网安装；电脑已有 WebView2 时可以离线安装。

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
- 输入调整目标时保持播放；发送有效指令后暂停回放，由所选模型提出结构化修改，通过参数和案例约束校验后创建新版本并从初始状态重新仿真。每次回复与版本记录实际使用的 AI 模型。
- 回放自然结束后，自动为当前版本请求完整重复实验；也可手动评估。比较产出率、平均在制品与单位能耗的均值和置信区间，查看方案说明、恢复历史方案，导出实验 ZIP 与独立 HTML 报告。

API 密钥按配置分别保留在进程内存中，退出后需要重新填写；地址、模型与配置列表会保留。密钥不会写入配置文件或实验导出包。没有 API 密钥也可以使用编辑、回放、评估和导出。

## 使用顺序

1. 打开本分支应用，点击“编辑模型”或左侧“模型”，修改初始输入；所有机器、缓冲区、班次和实验设置都在输入模型窗口中。
2. 点击“应用修改并预览”，或关闭编辑窗口后点击“运行预览”，进入全屏工作区。中央观察生产线，右侧查看动态指标，底部可暂停、重播、拖动进度和变速。
3. 需要 AI 调整时，在“模型接入”填写服务提供商的 API Base URL、模型和密钥并启用；右侧可随时切换配置。
4. 在运行助手输入目标，例如“保持缓冲容量不变，将第一台设备的可用率提高至 90%”。打字和切换模型不会暂停；点击发送或按 Ctrl+Enter 后暂停，校验成功的新版本自动重新运行。请求失败时，原回放仍可手动继续。
5. 回放自然结束后自动计算完整评估，完成后点击“查看最终结果”；也可随时通过“评估”入口手动评估当前方案。查看最终指标、修改记录与条件一致的历史对照，导出结果报告或完整实验。

![最终方案与运行评估](docs/images/workspace-results.png)

完整评估就绪时显示提示，不会自动弹窗抢走焦点。全屏模式保留助手、指标和控制按钮；不支持浏览器 Fullscreen API 的桌面宿主使用占满应用视口的沉浸布局。

软件“帮助”菜单内也有使用说明、软件信息和组件许可。完整说明见 [桌面版使用说明](docs/desktop.md) 与 [模型和仿真说明](docs/studio.md)。

## 数据与边界

数据保存在 `%LOCALAPPDATA%\SimPy Lab Studio`；可从“文件 → 打开实验数据目录”进入。软件旁不产生数据文件。替换 EXE 不会删除实验记录；关闭窗口后未完成的任务下次需要重新运行。

暂停控制的是已计算采样帧的回放，不会冻结后台正在计算的 SimPy 进程。每次 AI 调整会从初始状态运行新模型，不会把加工到一半的工件直接转移到新模型。预览可能只覆盖完整实验的一段时间；正式评估按所设完整时长与重复次数计算，结果单独显示。当前是串联制造线的二维离散事件仿真，不包含任意流程拖拽建模或写实三维工厂。工程应用需结合实际生产数据校准；详见 [案例一参数与假设](docs/paper_case_a.md)。

## 从源码运行与构建

需要 Windows、Python 3.11+（发行版使用 Python 3.13 x64）。

```powershell
git switch frontend/simulation-workspace
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
