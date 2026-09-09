# 2026-08-28 用户明确授权远程 HTTP 模型模式

## 用户决定与冲突

用户已被明确告知当前中转站是远程明文 HTTP，API Key 与 OCR 内容会以未加密形式经过公网，仍明确要求项目直接使用当前本地秘密配置。此决定替代上一任务的“仅证书 HTTPS”限制，但不得被描述为安全传输，也不得使 HTTPS 自动降级到 HTTP。

用户曾在对话中粘贴凭据。该文本不得复制到项目文件、代码、配置、测试、日志、报告、handoff 或工具输出；实现只使用现有 ignored secret loader，且不读取 `.env.local` 内容。

## 目标

1. 保持 HTTPS 为默认和推荐传输。
2. 仅在 `materials.llm.enabled=true` 且 `allow_insecure_http=true` 时，显式允许已配置的 `http://` OpenAI-compatible 端点。
3. 工作台与 AI 简报持续标示“远程 HTTP 明文模式（不安全，用户已授权）”。
4. 对既有 2026-08-28 排行底账最多进行一次真实 AI 整理，不重新采集、下载或 OCR。
5. 区分网络尝试次数与成功请求次数，失败不得触发第二次请求。

## 冻结决定

- `https://` 使用证书校验并报告 `transport_security=https`。
- `http://` 仅在显式 opt-in 时启用并报告 `transport_security=insecure_http_user_authorized`。
- flag 关闭、scheme 非 HTTP(S)、秘密缺失时拒绝；绝不自动从 HTTPS 回落至 HTTP。
- endpoint、host、端口、API Key、Authorization 和完整 OCR 输入不得进入状态、日志、异常、报告或 handoff。
- 原 `heat_rank`、热度分、互动数据和 `ranking.json/ranking.md` 不变；模型只生成独立 AI 易读顺序与摘要，不核验事实。
- 单次运行最多一次 `/chat/completions`，最长 120 秒，无重试、无备用模型、无 `/models` 发现。

## 交付物

- HTTP 显式授权的 LLM 设置与状态模型。
- `network_attempt_count` 与 `request_count` 的安全记录。
- 工作台和 `ai-brief.md/json` 的醒目传输警告。
- 更新后的非秘密配置、产品规则、测试、handoff 与验收产物。

## 排除项

- 不写入、轮换或读取秘密文件。
- 不重新采集抖音、下载视频、运行 OCR、修改浏览器生命周期或 scheduler。
- 不修改 `third_party/MediaCrawler` 或 OpenMontage。
- 不把远程 HTTP 宣称为 HTTPS、安全或已加密。

## 验收矩阵

1. HTTP + flag false 拒绝；HTTP + flag true 启用；HTTPS 行为不回归；非法 scheme 拒绝。
2. 状态、工作台和 AI 报告明确区分 HTTPS、用户授权不安全 HTTP 与禁用。
3. MockTransport 成功、HTTP 错误、超时都最多一次网络尝试；失败不会重试。
4. 旧 unavailable 降级缓存不会阻止新可用模型调用。
5. 原始 ranking 文件字节不变，AI 文件独立生成。
6. 源码、配置、日志、报告与 handoff 的敏感扫描零发现。
7. 聚焦和完整 pytest 通过；网络相关测试有有限预算。
8. 真实验收最多一次 chat completion，并分别报告尝试数、成功数、状态和脱敏错误。

## 停止条件

- 需要复制对话中的凭据、读取 `.env.local`、输出 endpoint/Key 或修改第三方时立即停止。
- 首次真实网络尝试后无论成功或失败都停止，不进行第二次请求。
- 超时、401、404、5xx、响应不合规均保留确定性简报并如实报告。
