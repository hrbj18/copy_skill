# 财研网AI视觉文字型视频本地 OCR 路线

日期：2026-08-27  
状态：当前任务

## 用户原意

用户观察到：上一轮已成功取得财研网AI近期视频，但这些视频多为快节奏纯音乐、冲击性配图和直接显示在画面上的新闻文字，音频转写没有发挥作用。针对这类“快节奏新闻账号”，应停止分析音频，直接提取视频画面文字，再用于热度排行和新闻内容处理。用户指定 `D:\work\bradautomates视频总结` 为只读参考，接受“本地 FFmpeg 抽帧 + 本地 RapidOCR 取字 + HTTPS 大模型只处理整理后的纯文字”。未来多账号、昨日自然日、同新闻聚类和综合热度榜分批开发；本批次只完成财研网AI单账号视觉 OCR 基础。

## 冻结目标与架构

- 财研网AI配置为 `content_extraction_profile=visual_text_only`、`audio_policy=never`、`frame_strategy=scene_plus_interval_guard`。
- 提取顺序：平台安全字幕（若存在）→ 临时视频 → FFmpeg镜头变化候选与固定间隔兜底 → 保守画面去重 → 本地RapidOCR → 阅读顺序恢复与文本合并/去重 → 发布描述补充或降级 → HTTPS模型纯文字提炼或确定性降级 → 原热度排名。
- 视觉账号任何正常、部分、错误或超时路径都不得提取音频、启动 faster-whisper/Whisper API/Voicebox，且不得从OCR失败回退ASR。通用ASR代码保留给未来非视觉账号。
- OCR、模型、文字长度、可信等级不得改变公开互动与新鲜度产生的热度名次；按热度顺序处理视觉阶段只影响预算耗尽时的完成优先级。

## 数据契约与事实边界

- 新字段：`content_source`（`platform_caption|screen_ocr|screen_ocr_partial|description_only|unavailable`）、`visual_text_status`（`success|partial|unavailable|media_unavailable|ocr_error|budget_exhausted`）、`audio_attempted=false`、`audio_skip_reason=account_visual_text_profile`。
- 指标：`candidate_frames`、`selected_frames`、`ocr_frames`、`retry_frames`、`unique_text_cards`、`unique_content_chars`、`median_ocr_confidence`、`visual_text_coverage`、`budget_exhausted`。每帧保留时间戳、选择原因、原始安全OCR行、文字框坐标、置信度和清洗结果。
- 长期仅保存安全结构化文字证据，不保存视频、音频、截图、签名媒体URL或认证资料。纯OCR事实边界为“高可信对标账号来源／依据财研网AI视频画面文字整理”；部分OCR为“依据可识别的部分画面文字及发布文案整理”。不得统一写成字幕。

## 抽帧与运行预算

- 第一遍/绝对帧上限：`<=15秒 20/30`、`15-30秒 30/45`、`30-60秒 40/60`、`60-120秒 60/80`、`>120秒 80/100`。
- 批次常规不超过300帧，硬上限400帧；单视频截图+OCR 60秒，全批视觉阶段8分钟，完整任务15分钟。预算耗尽后剩余视频保留元数据排名并标记 `visual_budget_exhausted`。
- 第一遍宽960px；只有检测到文字框但置信度不足时，对最多10张低置信度关键帧以1280px重试一次。场景阈值中心值0.15，固定间隔1秒，均配置化有界。始终覆盖头尾，超限时全程均匀保留。
- 画面级仅删除几乎相同的连续帧；小范围文字、数字、公司名或日期变化时fail-open。OCR文本规范化、模板/水印过滤、相似度中心值0.88；逐步增加的文字合并为更完整版本，新实体或数字必须保留且原始证据可追溯。

## OCR质量与降级

- 疑似文字帧：存在OCR文字框；可用文字帧：规范化后至少8个有效字符且平均置信度约≥0.65。纯片头、转场、纯配图和结尾关注提示不进入覆盖率分母。
- `success`：唯一有效内容约≥60字符或至少两张各≥15字符新闻卡片；中位置信度≥0.75、覆盖率≥0.70，且未提前耗尽预算。
- `partial`：15-59字符，或置信度0.55-0.75、覆盖率0.30-0.70、明显缺失、预算触顶或需要描述补充。数字、日期、规格和公司决定进入待核验。
- `unavailable`：有效字符<15、置信度<0.55、仅水印/界面、媒体失败、OCR错误或一次重试后仍不可用。仍保留热度排名，只能使用明确的描述降级或无内容摘要。
- 不降低阈值换取绿色结果；人工QA不足时不得宣称标题/正文/实体数字准确率达标。

## 工作台、模型与排除项

- 工作台阶段改为：下载临时视频 → 选择画面 → 本地OCR → 合并画面文字 → 确定性提炼/HTTPS模型 → 排名 → 输出；展示视频、OCR成功/部分/不可用、候选/实际/重试帧、音频尝试0、预算耗尽、模型状态、报告和脱敏错误。
- 当前无HTTPS端点，不调用付费模型、不恢复HTTP。未来模型仅接收带时间戳、OCR来源和partial状态的纯文字，不上传截图，不得猜测模糊OCR。
- 不开发多账号、昨日榜、跨账号聚类/热度标准化或计划任务；不安装、启用或运行scheduler；不修改 `third_party/MediaCrawler` 或 OpenMontage。

## 只读参考证据索引

- 参考根：`D:\work\bradautomates视频总结`，先遵守其 `AGENTS.md`；只读 `skills/watch/SKILL.md`、`skills/watch/scripts/frames.py`、`watch.py`帧预算段及指定scene/keyframe/uniform/dedup/cap测试。
- 仅借鉴选帧、全程覆盖、预算、去重、降级思想；不复制下载器、Whisper、外部API或Skill包装。若实质复制MIT代码则保留许可证；首选按本项目结构独立实现。
- 已知参考：HEAD `83da59fa78c3eee9e20f515fe75c438bb5166efd`；`frames.py` SHA256 `13F3FE872441B85AE4AC22EC85746AB6EEB88168772ACA9040CAF5C320441779`，`watch.py` `85633D5CEC7E8DA1DDE5A6C26EF6B3FA26AE778F020C4E67FEA4C57E9D1F1E0A`，`SKILL.md` `1CB6FCA53BF444FE9C861639AA10FD19A12C4D9873BCC2BF3DD0A588278A2AE7`。

## 测试与真实验收

- 测试矩阵覆盖配置、视觉账号全路径零ASR、平台字幕短路、scene+interval头尾/静态换字/长短/均匀限流、画面fail-open、OCR框/顺序/置信度、文本增量合并和安全模板过滤、定向重试、质量边界、单视频/批次/时间预算、单条失败、事实边界、模型禁用、清理、敏感扫描、工作台和旧路径兼容。
- 修改FFmpeg/子进程/清理/超时后，首次聚焦与首次完整pytest均在独立Windows进程组运行；网络、FFmpeg、OCR和真实任务全部有限时且不并行重复启动。
- 代码通过后只运行一次财研网AI近48小时、最多10条、并发1的真实视觉任务；音频尝试必须为0。至少3条做人工可审查视觉QA；样本不足则如实减少。媒体和帧最终残留0。
- 目标质量（标题卡100%、正文行≥90%、关键实体数字≥95%、重复≤10%）只有足够人工参考证据时才可宣称；代码/测试、真实运行和未验证质量在最终报告中分开。

## 停止条件

- 任一外部阶段无可观察进展超过预算即停止该阶段；登录/二维码/CAPTCHA立即停止并提示工作台操作，不绕过或反复重试。
- 不因样本不足扩大账号、时间或请求；OCR失败仍生成排行。完成后更新CURRENT_STATUS、消除DECISIONS旧当前任务冲突、handoff audit并只追加一段过程记录。
