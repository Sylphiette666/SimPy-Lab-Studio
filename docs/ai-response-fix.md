# AI 响应完整性修复

运行助手曾统一显示“LLM 未返回完整的参数修改，请调整提示词后重试”，无法区分输出截断、空正文和其他未完成状态。用户报告使用 DeepSeek 的 Responses 接口发送“提高产出率”失败；这类目标在现有提示词约束内是允许的。旧版没有保存该次失败的原始响应，不能据此确定服务端失败原因。

旧请求固定最多生成 2400 token。按 DeepSeek 官方说明，思考模式默认开启，思考 token 也计入输出上限。因此旧设置存在思考用尽额度、参数正文尚未完成的风险。

## 修改

- 参数调整输出上限改为 8192 token。仅对 `api.deepseek.com` 官方地址显式关闭思考模式：Responses 使用 `reasoning.effort=none`，Chat Completions 使用 `thinking.type=disabled`。不依赖配置名称或模型名称判断供应商，不改变用户保存的地址、模型或协议。
- Responses 仍请求严格 JSON Schema，改为先检查响应和消息是否完成，再校验最终正文，避免 SDK 提前解析截断 JSON 后遮蔽具体原因。忽略 reasoning 内容，不从思考过程、代码块或工具调用中猜测参数。
- 分别提示输出上限、过滤、拒绝、空正文、格式错误及请求参数不被服务接受；不显示供应商原始错误正文或密钥。未完成输出即使碰巧是合法 JSON，也不会应用。
- 现有参数白名单、完整模型校验、论文模式限制、新版本保存和重新仿真流程保持不变。SimPy 引擎、指标计算和桌面宿主未修改。
- 没有新增自动修复请求或自动切换接口。现有 SDK 网络错误重试策略保持不变。

“模型接入”中显示 DeepSeek 非思考模式说明。用户仍可输入宽泛目标，由模型提出带有假设的参数试验；是否改善以真实仿真结果为准。

## 验证与边界

`tests/test_studio_ai_transport.py` 使用真实 OpenAI SDK 和 `httpx.MockTransport`，验证两种协议的请求体、正常 JSON、只有思考/无正文、截断 JSON、截断但合法 JSON、过滤、拒绝、格式错误和 HTTP 错误。不会访问真实服务，也不使用用户密钥。原有 AI、配置及 Studio 路由测试继续验证参数约束和失败时的模型保护。

这证明本地协议处理和保护逻辑符合测试场景，不表示已验证用户账号、模型可用性、网络连接或实际优化质量。已保存的模型名称保持原样；服务商若更改模型标识，应按其控制台提供的标识填写。

## 官方依据

核对日期：2026-09-14。DeepSeek 文档当时的部分模型示例与搜索缓存存在别名差异，本次没有据此替换用户选择的模型。

- [DeepSeek Responses API](https://api-docs.deepseek.com/zh-cn/api/create-response/)：输出上限包含思考 token；响应提供完成状态和截断原因；支持 JSON Schema。
- [DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)：默认开启思考，以及两种接口的关闭方式。
