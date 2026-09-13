# 单次开发指导：资源受控的语义素材流水线

## 背景与目标

当前项目已经能够真实采集三个抖音账号、下载 Top N 视频、使用本地 faster-whisper 生成时间戳转写并输出 Markdown，但仍存在两个关键缺口：视频默认长期保留会导致存储持续增长；摘要页只是索引，转写也没有经过大模型的分段理解与高价值提炼。本次开发将媒体改造成短生命周期中间产物，并在严格资源上限内加入 OpenAI-compatible 中转站语义分析，使最终长期资产以结构化转写、分析 JSON 和 Markdown 为主。

## 冻结的优化点

1. **临时视频默认策略**：下载的 MP4 默认是临时文件；仅显式 `--keep-video` 或配置允许时才长期保留。
2. **音频优先**：MP4 校验后提取 16kHz 单声道压缩音频或分片音频，提取成功立即删除临时 MP4；转写结束后默认删除音频。
3. **低内存顺序执行**：媒体下载、音频提取、ASR 和大模型分析并发均固定为 1；长内容按小块顺序处理，不把整条长音频或全部转写一次装入模型上下文。
4. **ASR 分片持久化与续跑**：以固定时长切片，逐片写入 checkpoint；失败后只重跑未完成片段。最终合并为带绝对时间戳的 transcript JSON。
5. **媒体与缓存生命周期**：配置临时目录、软磁盘配额、文件 TTL 和自动清理；长期保留原始采集 JSONL、transcript、analysis、Markdown；成功后清除临时 MP4、音频分片和 OCR 图片。
6. **中转站秘密隔离**：API Key 只允许来自 `DOUYIN_LLM_API_KEY` 或被 `.gitignore` 排除的本机私密环境文件；不得写入项目 JSON、Skill、日志、Markdown、运行报告、测试或错误消息。Base URL 和模型名可以是非秘密配置。
7. **模型发现与健康检查**：支持 OpenAI-compatible `/models` 和 `/chat/completions`；模型未显式配置时可从 `/models` 选择，选择结果写入不含秘密的运行信息；提供只报告状态、不回显密钥的诊断命令。
8. **长转写分段分析**：按时间戳和字符预算切分，逐段输出严格 JSON：主题、摘要、核心观点、重要数字/事实主张、价值片段及时间戳、叙事技巧、可延伸选题、待核验事项。
9. **层级合并**：只将各段结构化分析送入最终合并，去重并形成视频级高价值分析；超长内容继续分批归并，避免单次上下文失控。
10. **摘要升级**：`summary.md` 直接展示每条视频的价值摘要、核心信息、最佳时间片段、可延伸方向和核验提醒，不再只是标题链接；逐视频 Markdown 保留完整时间戳转写与分析。
11. **无语音分支**：无语音视频进入资源受控关键帧 OCR。按镜头或固定低频抽帧，OCR 后立即删除图片；OCR 不可用时明确标记 partial，不伪造正文。
12. **可恢复与失败隔离**：单视频或单片失败不破坏已有成功产物；原子写入；可根据 checkpoint 续跑；LLM 不可用时保留转写并标记 partial。
13. **下游保留原件入口**：支持显式将已选视频标记为需要原件；默认日常分析不长期保存媒体。此次不写入 OpenMontage。

## 实现决策

- 保留 MediaCrawler 为结构化元数据和临时媒体 URL 提供者，不修改 `third_party/MediaCrawler`。
- 使用 ffmpeg/ffprobe 处理媒体；ASR 继续使用本地 faster-whisper base/CPU/int8/2 threads。
- 音频分片建议 180 秒；以每片 JSON checkpoint 支持续跑。
- 本机若有 Tesseract 则使用其简体中文/英文 OCR；否则实现可诊断、可跳过的 OCR 分支，并允许后续视觉模型补充。OCR 只处理无语音或明确要求的候选。
- 中转站通过项目层 HTTP 客户端调用；提示词要求 JSON，客户端对代码围栏、非法 JSON、超时和限流进行有限重试。
- LLM 分析缓存键必须包含 transcript 内容哈希、模型和分析版本，防止提示词或转写变化后误用旧缓存。
- 不把抖音字幕或模型总结当成事实证据，所有事实主张必须进入待核验列表。

## 配置与秘密

非秘密配置放入 `config/content_intelligence.json`：

- `materials.retention`：keep_video、keep_audio、temp_root、quota_bytes、ttl_hours。
- `materials.transcription.chunk_seconds` 与 checkpoint 目录。
- `materials.ocr`：enabled、frame_interval_seconds、max_frames、语言。
- `materials.llm`：enabled、base_url、model、chunk_chars、timeouts、retries、analysis_version。

秘密只从环境读取：

- `DOUYIN_LLM_API_KEY`
- 可选 `DOUYIN_LLM_BASE_URL`
- 可选 `DOUYIN_LLM_MODEL`

## 交付物

- 资源生命周期、临时媒体、分片 ASR checkpoint、清理和配额模块。
- OpenAI-compatible LLM 客户端、模型发现、分段分析、层级合并与缓存。
- 无语音关键帧 OCR 分支。
- 升级后的 summary.md、逐视频 Markdown 和 run_report.json。
- CLI 参数和 doctor/LLM 诊断入口。
- 单元测试、模拟服务集成测试和至少一次现有真实采集数据的端到端验证。
- 更新 Skill 路由与数据契约。

## 明确不做

- 不访问或修改 `D:\codex_work\OpenMontage`。
- 不实现每日计划任务。
- 不绕过验证码、登录挑战或平台风控。
- 不下载全部历史视频、不长期保留默认 MP4、不获取评论。
- 不把 Cookie、API Key 或临时签名 URL写入产物。
- 不依赖付费 OCR 服务；中转站仅用于已授权的文本/可选视觉分析。

## 验收标准

1. 默认成功运行后，媒体目录不保留 MP4、音频或 OCR 图片，只保留小型 checkpoint/文本缓存；显式 keep-video 时才保留 MP4。
2. 180 秒以上音频分片逐片落盘；人工模拟中断后能复用已完成分片，不重复 ASR。
3. 磁盘配额和 TTL 清理有单元测试，绝不删除配置根目录之外的文件。
4. API Key 不出现在配置、日志、Markdown、报告、Skill、测试快照或命令计划中。
5. LLM 分段分析和最终合并均使用严格结构，失败可诊断；缓存命中不重复请求。
6. summary.md 至少包含价值摘要、核心信息、最佳时间戳、延伸方向、核验事项与详情链接。
7. 无语音候选进入 OCR；有 OCR 时生成带时间戳文本，无 OCR 时状态明确为 partial。
8. 使用现有 `real-20260826-v3` 原始数据重建材料，三条候选都生成文档；至少一条长视频成功完成分段分析。
9. 全部项目测试和 Skill `quick_validate.py` 通过；MediaCrawler 上游工作区保持干净。
