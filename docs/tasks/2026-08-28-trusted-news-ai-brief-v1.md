# 2026-08-28 可信账号 AI 易读简报 V1

## 用户需求与真实基线

用户已完成财研网AI的一次真实登录采集，希望启用已有安全中转站模型，对 OCR 文本与公开互动数据做一次批量分析、阅读顺序调整和易读化整理；模型不负责新闻真实性判断，也不联网查证。本次真实底账为：

- `output/trusted-news-accounts/caiyan-ai/2026-08-28/ranking.json`
- `output/trusted-news-accounts/caiyan-ai/2026-08-28/ranking.md`
- raw=10、48小时内=6、processed=6、screen_ocr=6、OCR 109帧、audio_attempts=0、采集 returncode=0、安全扫描0发现。
- 原报告为 partial 的唯一原因是 HTTPS 模型未启用；安全状态 helper 只证明三个模型环境项已配置，不授权读取或打印其值，也不得读取 `.env.local`。
- 用户授权仅对此报告做至多一次真实 chat completion，不授权重复付费、自动换模型、事实核验或扩大采集。

## 目标

1. 保留现有 `heat_rank`、热度分、互动数据和原始报告作为不可变、可复算底账。
2. 新增独立 `ai_editorial_order`，由模型在最多10条安全输入间安排易读顺序，但不得覆盖热度名次。
3. 生成同目录、独立的 `ai-brief.json` 与 `ai-brief.md`，明确“未核验新闻真实性”。
4. 工作台可信账号区域提供状态、自动整理复选项、单独整理按钮、打开本次报告与打开文件夹能力。
5. 收尾采集期项目浏览器单页面/单窗口管理，并诚实区分 CDP 页面证据与仍需用户确认的 OS 可见窗口行为。

## 冻结产品决定

- 原始热度排序永远是确定性底账；AI 只产生独立阅读顺序和文字组织，不得修改输入数字、事实或来源边界。
- 模型不联网、不检索、不核验、不增加 `verified`、真实性评分、官方结论或 OCR 外部事实。
- Markdown 同时显示 AI 顺序和原始热度名次；差异理由仅基于输入的互动、时间、OCR完整度、代表性与内容差异。
- 统一声明：`以下内容仅为对标账号视频与公开互动数据的 AI 整理，未核验新闻真实性。`
- `claims_to_verify`/`do_not_claim` 可保留在机器输出或附录，但主简报聚焦排序、归纳与可读性。

## AI 请求与数据契约

- 单次运行最多一个 `/chat/completions` 请求；低温度、有限输入、有限输出、总超时不超过120秒，无自动重试、无备用模型。
- 只调用现有布尔安全状态和安全 analyzer 接口。严禁读取/打印/持久化 endpoint、API Key、Authorization、Cookie、签名媒体地址、profile、CDP/WebSocket信息、日志或临时媒体路径。
- 输入只含稳定 video_id、原始 heat_rank/heat_score、公开互动、发布时间、安全 OCR/字幕文本、确定性标题/摘要、OCR状态。
- 输出结构至少含：schema/version、`analysis_role=editorial_organization_only`、`verification_performed=false`、daily_overview、themes、editorial_items、analysis_metadata。
- 校验未知/重复 ID、越界 index、名次/分数/互动篡改、输入外新增数字事实、长度和漏项。失败或漏项时不得编造，按原始热度顺序生成确定性降级简报。
- `analysis_metadata` 只保留安全模型名、prompt/schema版本、request_count、cache_hit、生成时间，不包含 endpoint 或密钥。

## 缓存与产物

- 内容哈希由安全输入字段、模型名、prompt版本、schema版本产生。相同哈希优先复用合规的现有 `ai-brief.json`，不再请求模型。
- 原 `ranking.json/ranking.md` 不覆盖、不重排。AI状态和路径优先写入 job state，不要求修改底账。
- 产物路径固定为底账同目录的 `ai-brief.json`、`ai-brief.md`；失败也生成明确的确定性降级版本。

## 工作台范围

- 在“可信账号科技快讯”区域增加：模型状态标签、默认开启的“采集完成后自动进行大模型整理”、`用大模型整理本次报告`、`打开本次报告`、`打开报告文件夹`。
- 手动整理只读取最新 `ranking.json`，不得重新采集、打开浏览器、下载媒体、运行 OCR 或启动 scheduler。
- 采集结束自动整理前仍必须通过 HTTPS 与 Key 布尔状态检查；失败显示“内容提取完成；大模型整理已降级”而不是把采集说成失败。
- 完成提示包含输出路径、处理数量和模型成功/缓存/降级原因。

## 浏览器闸门 0 收尾

- 不修改 `third_party/MediaCrawler`，只在项目 adapter/config/tests 中定位并收敛采集期间新增页面。
- 项目专用 CDP 仍限定配置端口/profile；不得碰个人 Chrome、按进程名终止、删除持久 profile 或输出页面URL/target认证信息。
- 采集过程中尽量保持最多一个可见项目页面；记录安全的 target数量和可识别窗口归属证据。OS窗口数无法自动证明时明确保留人工复验项。
- 正常结束关闭项目浏览器；登录/QR/CAPTCHA保留一个页面；租约冲突不得关闭他人任务实例。

## 非秘密配置

- `materials.llm.enabled=true`。
- `allow_insecure_http=false` 保持不变；只允许证书校验通过的 HTTPS OpenAI-compatible端点。
- 工作台自动AI整理默认开启；不得把任何秘密写入配置或报告。

## 排除项

- 不做多账号、昨日自然日聚合、跨账号事件聚类、推荐流扩展或计划任务。
- 不重新采集本次真实报告，不下载视频，不重跑 OCR，不调用视觉模型。
- 不做新闻联网核验，不写入 OpenMontage，不修改第三方 checkout，不安装/启用/运行 scheduler。

## 测试矩阵

1. 批量 schema、未知/重复 ID、越界 index、数字篡改与输入外数字事实拦截。
2. 一次请求预算、无重试/无模型回退、严格 HTTPS、证书校验与API失败降级。
3. 相同内容哈希命中缓存且 request_count=0；模型成功 request_count=1。
4. 原始 heat_rank/score/互动不变；原始 ranking 文件字节不变；AI文件独立生成。
5. 漏项/不合规响应产生完整但克制的确定性降级，不冒充模型成功或事实核验。
6. UI状态、自动开关、独立整理按钮、报告/文件夹路径和部分成功措辞正确；手动整理无浏览器/OCR/采集副作用。
7. BrowserSession复用、采集期单页面、登录保留、正常关闭、租约冲突和有限超时。
8. 首次浏览器/进程相关聚焦与完整 pytest 均在隐藏独立Windows进程组运行。
9. 真实 BAT GUI 有界冒烟不触发模型、浏览器或采集。
10. 敏感扫描、原报告完整性、scheduler只读未安装和handoff audit通过。

## 真实验收与请求预算

1. 离线 MockTransport/fixture 全绿后，只对 `2026-08-28/ranking.json` 运行一次真实AI整理。
2. 真实生成预算最多1次 chat completion、最长120秒、不得自动重试或换模型；不调用抖音采集、下载或OCR。
3. 若 HTTPS/证书/Key/API/JSON校验失败，立即停止真实调用并生成确定性 `ai-brief`，报告脱敏原因。
4. 记录真实 request_count、cache_hit、模型状态与产物绝对路径；静态/Mock结果不得冒充真实成功。

## 停止条件与最终分层报告

- 任一网络/模型/浏览器阶段超出预算或无可观察进展即停止，不重复付费请求。
- 若真实报告缺失、底账哈希意外变化、需要读取秘密或必须修改第三方，停止并报告具体阻塞。
- 最终按“真实实现 / 离线测试 / 真实模型调用 / 仍需用户复验”四层报告，并列出任务指南、主要代码、测试数、产物路径、请求次数、浏览器证据和所有未完成项。

