# 模型接入窗口更新

2026-09-16，当前滚动测试版，相对于 v1.2.0 正式版。

## 密钥显示

- 重新打开设置时，读取当前所选配置的真实密钥并填入密码框，默认以圆点遮挡；“显示 / 隐藏”按钮可切换查看。
- 要在退出整个软件后保留密钥，仍须勾选“在本机加密保存密钥”并保存。仅关闭设置窗口不会丢失本次运行的密钥。
- 回填的密钥不会被当成用户新输入的替换密钥。更改服务地址时清除旧服务回填值；同一地址切换模型可保留密钥；复制配置不复制密钥。
- 关闭窗口、切换配置、开始输入替换密钥或改变地址后，迟到的读取结果不会覆盖当前输入。密钥只存在于本次窗口内存与密码框中，不加入 localStorage 或实验导出。

## DeepSeek Flash

“快捷填写服务”增加推荐名称和兼容名称两种选择。也可手动填写：

| 字段 | 填写内容 |
| --- | --- |
| API Base URL | `https://api.deepseek.com/v1` |
| AI 模型名称 | `deepseek-v4-flash`，或推荐的 `deepseek-flash` |
| 接口协议 | `Chat Completions` |
| API Key | DeepSeek 平台提供的密钥 |

截至核对日期，官方仍接受 `deepseek-v4-flash`，但旧模型已退役，该名称的请求转至 DeepSeek-V4.1-Flash；推荐的新名称为 `deepseek-flash`。现有配置不会自动改名，用户选择模板或手动修改后点击“保存并启用”生效。

请求沿用 `/v1/chat/completions`、JSON 输出及 `thinking: {type: "disabled"}`，用于生成待确认的参数修改建议，应用前仍须进行参数校验。

官方依据：[API 说明](https://api-docs.deepseek.com/)、[2026-09-10 更新说明](https://api-docs.deepseek.com/news/news260910/)、[官方 /v1 配置示例](https://api-docs.deepseek.com/quick_start/agent_integrations/workbuddy/)、[思考模式控制](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 验证范围

使用假密钥、隔离测试目录和模拟模型响应验证，不使用用户真实密钥、不请求付费服务。覆盖重启恢复后的专用密钥读取、访问边界、无缓存、普通返回及导出隔离，两种模型名称的实际 SDK 请求序列化，以及浏览器回填、遮挡、配置切换、地址变更与迟到响应。

本次已通过 286 项 Python 测试、Ruff 检查，以及密钥交互、加密保存、连接配置、工作区和可靠性浏览器回归。窗口截图检查覆盖 1440、960、390 像素宽度。打包 EXE 自检验证嵌入资源、实际 SimPy 预览、后台重启后密钥恢复与设置读取、密钥移除、后台进程退出。

更新后的测试 EXE SHA-256：`8122e9fa72b28d09a11d4ea1b21c294087af45f8b5e7780c27024a9bba96702b`。测试版交付位置维持 `F:\Simpy\SimPy Lab Studio 测试版\SimPy Lab Studio.exe`；没有发布新的正式版本。
