# 2026-08-28 单条已确认新闻素材探针 V1

## 目标与阶段边界

在候选账号池 V1 已完成的基线上，为一条已经由官方一手来源确认的科技新闻生成最小、可审计、可供后续 OpenMontage 接入的静态素材包。本阶段只验证“从确认新闻到素材清单”的技术路径，不修改 OpenMontage，不开发批量新闻素材搜索，不下载任意网络视频，不让模型判断新闻真假。

首个真实探针固定为 Bill Gates 官方文章《The choices we make about AI now are critical》。确认来源为 `https://www.gatesnotes.com/a-turbulent-ai-era-and-critical-choices-to-make`。项目只保留与官方文章直接相符的中性摘要：Bill Gates 讨论了 AI 转型可能带来的剧烈变化、社会准备不足，以及对 AI token 和机器人征税等治理选项。不得把抖音文案或二手报道当成事实证据。

## 当前兼容基线

- 候选账号池、财研网AI采集、视觉 OCR、确定性热度底账和 AI 易读简报保持不变。
- 素材探针不调用 LLM、OCR、ASR、浏览器自动化或 MediaCrawler。
- `CopySkillDailyTechNews` 保持未安装、未启用、未运行。
- 不读取 `.env.local`、Cookie、浏览器 profile 或任何秘密。
- 不写入 `D:\codex_work\OpenMontage`；全部产物只落在本项目 `output/material-probe/`。

## 输入与确认门

输入为项目自有、无秘密的故事 JSON，至少包含：

- 稳定 `story_id`、中文标题和日期；
- `confirmation_status=official_primary_source`；
- 至少一个 HTTPS 官方来源 URL；
- 仅由已列官方来源支持的简短中性摘要；
- 可选的明确许可公共素材查询词。

若没有官方一手来源、URL 不在允许域名、使用 HTTP、来源指向本机/私网，或确认状态不满足要求，探针必须拒绝运行，不能用社交平台热度补足确认门。若已冻结的官方链接仅因 401/403/429 反自动化策略无法由本机读取，不伪装浏览器或绕过限制：跳过官方页面图片，只继续一次预批准 Commons 查询与本地来源卡，最终状态最高为 `partial`。

## 有界获取合同

- 整次运行最多访问 10 个网页/API 请求，最多保存 5 个静态素材。
- 总下载上限 50 MB，墙钟预算 5 分钟；单请求连接与读取超时不超过 15 秒。
- 仅允许 HTTPS GET，固定允许域为官方新闻来源域与 `commons.wikimedia.org` / `upload.wikimedia.org`。Windows 代理/TUN 使用 `198.18.0.0/15` 与本机 `fd7a:746f:6c69:6e65:66::/96` Fake-IP 时，只允许“白名单域名解析结果”通过配置中这两个精确网段；URL 直接填写 IP 仍拒绝，并继续依赖 HTTPS 证书校验。
- 只接收经过实际解码验证的 PNG、JPEG、WebP；拒绝 HTML 冒充图片、动画、视频、音频、SVG 和未知类型。
- 单素材最大 15 MB，最小边长默认 480 像素；生成卡片不计网络请求，但计入 5 个素材上限。
- 对下载内容计算 SHA-256 与感知哈希；完全重复或近似重复只保留一个。
- 不持久化带 token、signature、auth、expires 等敏感查询参数的 URL；错误文本与报告统一脱敏。
- 任一来源失败不扩大搜索范围、不无界重试；保存已取得的结果并返回 `partial` 或 `failed`。

## 来源与版权分级

每个素材必须保存稳定 `asset_id`、本地相对路径、来源页面 URL、可公开的直接资源 URL、来源类型、作者、许可名与许可 URL、权利状态、SHA-256、尺寸、文件大小、相关性说明和建议画面角色。

权利状态只允许：

- `project_generated`：本项目生成的标题卡/来源卡，可直接用于项目；
- `renderable_with_attribution`：Wikimedia Commons 明确给出允许复用的许可与作者，使用时必须署名；
- `review_required`：官方文章页面素材但未取得明确复用许可，下载用于内部评审，不自动进入可渲染目录；
- `reference_only`：许可未知、不兼容、尺寸不足或其他原因，只保留引用记录，不作为渲染输入。

此分类是工程工作流标记，不构成法律意见。官方页面图片默认 `review_required`，不能因为“官方”就标记为可商用。Commons 只有解析到受支持许可、作者和许可链接时才能进入 `renderable_with_attribution`。

## 素材包结构

输出目录固定为 `output/material-probe/<target-date>/<story-id>/`：

- `story.json`：冻结的确认新闻、来源与运行预算；
- `manifest.json`：机器可读素材、权利、哈希、尺寸、失败与预算统计；
- `preview.md`：中文预览、来源、署名要求、可用性和局限；
- `renderable/`：仅包含 `project_generated` 与 `renderable_with_attribution`；
- `review-required/`：保存需人工复核的官方页面素材；
- `reference-only/`：只保留明确需要保存的参考素材；
- `run.log`：无秘密的有界运行摘要，不记录响应正文或环境变量。

所有 JSON/Markdown 和最终目录通过临时目录加原子替换发布；失败也应留下结构完整、状态诚实的报告。稳定素材 ID 从规范化来源、内容哈希和角色确定生成，同一输入与同一内容不得漂移。

## 最小获取策略

1. 读取并校验故事输入，通过官方来源确认门。
2. 获取官方文章 HTML，只提取 Open Graph / Twitter 主图和有限文章图片候选；最多保存 2 个，均为 `review_required`。
3. 通过 Wikimedia Commons 官方 API 对单个查询词做一次有限搜索，再对有限候选读取元数据；最多保存 1 个许可明确、相关且尺寸合格的素材。
4. 本地生成标题卡和来源卡，包含新闻标题、官方来源域名与“素材探针/待编辑”标识，不生成或伪造新闻现场画面。
5. 去重、分类、写出素材包；不得为凑足数量下载无关图片。

## CLI 与工作台

增加确定性 CLI：

`python -m douyin_intelligence.cli material-probe run --story config/material_probe_story.json`

工作台只增加两个简单入口：运行默认最小素材测试、打开最新素材报告。运行仍使用既有单 worker/任务锁，重复点击应被拒绝，不创建第二次网络任务。失败在中文状态和日志中可见。

## 验收矩阵

1. 故事输入、确认状态、HTTPS、域名白名单、私网/本机和路径约束校验。
2. 10 请求、5 素材、50 MB、15 秒单请求、5 分钟墙钟预算均有硬限制。
3. MIME、真实图片解码、尺寸和单文件大小校验；HTML 冒充图片被拒绝。
4. SHA-256 与感知哈希去重；素材 ID 稳定。
5. 官方图片默认 `review_required`；未知许可不能进入 `renderable/`。
6. Commons 许可、作者、许可链接缺一不可；需署名素材在预览中明确显示。
7. 生成卡片只表达标题与来源，不伪造现场图；可重复生成且无秘密。
8. 产物目录原子发布，JSON/Markdown 内容一致，失败/部分成功状态诚实。
9. URL 与错误脱敏；报告不得包含 endpoint、Key、Cookie、Authorization、profile、WebSocket 或临时签名参数。
10. CLI、工作台入口、重复 worker 拒绝和既有候选池/排行榜/OCR/AI 简报回归。
11. 真实探针只运行一次；确认网络请求、素材数量、体积和时间未越界，OpenMontage 零写入。
12. doctor、聚焦 pytest、完整 pytest、敏感扫描、只读 scheduler 状态与 handoff audit 通过。

## 成功、降级与停止条件

理想成功为至少 3 个相关视觉资产，其中至少一个网络来源素材、至少一个可渲染素材，所有素材均有来源和权利分级。若只获得官方图片和生成卡片，或官方链接因 401/403/429 反自动化策略无法程序化读取，可返回 `partial`，并明确访问或版权限制；没有许可明确的外部素材不能伪装为完整成功。确认门失败，或官方来源发生 DNS、TLS、5xx 等非反自动化故障时，停止外部步骤并报告 `failed`。

完成一次真实探针、测试和交接维护后停止，不自动批量处理排行榜，也不修改 OpenMontage。后续是否进入多新闻素材服务由用户根据本次素材质量、版权可用性和运行成本另行决定。
