# PRD：主题驱动的抖音「素材复刻」采集与交付工作流

- 文档类型：增量 PRD（产品需求）
- 项目：`douyin-content-intelligence` v0.3.0
- 日期：2026-09-12
- 状态：待评审 / 待用户拍板
- 上游依据：`AGENTS.md`、`docs/handoff/PRODUCT_RULES.md`、`docs/handoff/RUNTIME_SAFETY.md`、`docs/handoff/CODE_MAP.md`
- 关联任务：Task #1（PRD）→ Task #2（架构）→ Task #3（实现）→ Task #4（验证）

---

## 1. 背景与目标

### 1.1 产品定位（一句话）

在既有「抖音科技内容情报供应层」之上，新增一条**主题驱动的素材复刻采集与交付工作流**：用户给一个主题，系统返回**可复用的高播放实拍片段 + 可借鉴的脚本结构**，替代当前唯一素材源 Pexels 的通用低质素材。

### 1.2 现状缺口（本次要补的三件事）

| 缺口 | 现状 | 本期要求 |
| --- | --- | --- |
| 人脸检测 | 完全没有 | 新增人脸存在性检测与「少人脸」排序，且**不得引入新原生依赖** |
| 主题化批量采集+下载 | 只有关键词搜索元数据 + 单个视频媒体 URL 下载 | 新增「主题 → 批量采集 → 批量下载实拍素材」工作流 |
| 按主题+日期的交付结构 | 只有按业务日期的素材包 | 新增 `MM.DD<主题>复刻视频/` 交付目录 + 清单文件规范 |

### 1.3 本期目标与优化优先级

| 优先级 | 优化目标 | 含义 | 度量 |
| --- | --- | --- | --- |
| 1 | **速度** | 用户需要「查看大量内容」，端到端不能等人 | 单主题端到端 ≤ 30 min（硬超时 45 min） |
| 2 | **准确度** | 挑出来的脚本复刻/素材复刻视频要真的可用 | 脚本复刻视频人工可用率 ≥ 60%；素材片段人工可用率 ≥ 50% |
| 3 | **少人脸** | 复刻视频不能出现其他博主的脸 | 交付片段 `face_heavy` 数量 = 0；主素材必须 `face_free` |

**优先级说明（重要）**：速度 > 准确度 是**算法调优与默认参数**的取舍方向；「少人脸」是**交付硬门槛（hard gate）**，不参与权重妥协——即在速度/准确度与「无人脸」冲突时，以排除人脸为准。此设定见 §7 Q4。

### 1.4 示例场景（用户原话还原）

> 一期「苹果折叠屏手机介绍」：挑 1 个高播放、脚本思路值得借鉴的视频（如"老师好我叫何同学"那种质量），借鉴其脚本思路写自己的脚本；再挑 2~4 个不同博主的**实拍片段**用来拼接。只用交付主素材、辅助素材、脚本思路；怎么拼接是下一步的事。同一期同类型视频放同一文件夹，如 `9.12苹果折叠屏复刻视频`。优先找真人出镜少的镜头——复刻视频不能出现其他博主的脸。

---

## 2. 用户故事

| # | 角色 | 我想要 | 以便于 |
| --- | --- | --- | --- |
| US-1 | 短视频创作者 | 输入一个主题（如「苹果折叠屏手机」），一次跑完得到交付目录 | 不用手动刷抖音挑素材 |
| US-2 | 短视频创作者 | 拿到 1 个高播放「脚本复刻视频」的结构化脚本骨架（含时间码分段） | 基于它改写自己的脚本，而不是照抄文案 |
| US-3 | 短视频创作者 | 拿到 2~4 个不同博主的实拍片段，且**画面里没有博主的脸** | 拼接时不会出现别人的脸 |
| US-4 | 短视频创作者 | 主素材/辅助素材分别放在固定子目录并有清单，每个片段标注「对应脚本骨架的哪一段」 | 下一步拼接时直接对号入座 |
| US-5 | 创作者/运营 | 系统明确告诉我哪些是证据、哪些是猜测（人脸指标、降级情况、警告） | 我不把抖音内容当事实依据 |

---

## 3. 需求池

> 验收标准均为**可观测、可自动断言**的描述。所有网络/浏览器/子进程操作沿用现有有界超时机制。

### 3.1 P0（必须）

#### P0-1 主题输入与关键词扩展

- 功能：用户以 `--theme "苹果折叠屏手机"` 传入主题；系统生成 3~6 个搜索词（主题直出词 + 同义/别名/品类词，如 `折叠屏手机`、`苹果折叠屏`、`iPhone Fold`、`折叠屏 折痕`）。
- 复用：`search_collector.controlled_keywords()` 的 10 词上限与去重逻辑。
- 验收：
  1. 给定主题必产出 ≥ 3 个非重复关键词；
  2. 关键词总数 ≤ 10，且派生词不得超出主题语义域（离线断言词表）；
  3. 关键词清单写入交付清单 `keywords_used`。

#### P0-2 主题化候选池采集

- 功能：复用 `collect_search()`（MediaCrawler `--type search --keywords "a,b,c"`）采集候选池，产出 `search_contents_*.json`，并归一化为候选记录（video_id / 标题 / 作者 / 播放 / 点赞 / 时长 / 发布时间 / media_url）。
- 验收：
  1. 单主题候选池规模落在 `[40, 120]`（默认目标 80，见 §7 Q1）；
  2. 采集总预算受 `hard_max` 约束，**实际请求上限不得超预算**（沿用 `raw_request_ceiling` 语义）；
  3. 采集耗时 ≤ 8 min（超时则返回 `partial` + 已有候选，不得抛异常终止全流程）；
  4. 输出 `candidate_pool.json` + `search_report.json`（含 status/budget/keywords/超时信息）。

#### P0-3 「脚本复刻视频」筛选（1 个）

- 筛选条件（须同时满足）：

| 条件 | 阈值 |
| --- | --- |
| 播放量 | 候选池内 `heat_score` 排名 Top 10%（至少 Top 5） |
| 口播密度 | ASR 文本 ≥ 150 字且 字数/时长 ≥ 1.2 字/秒 |
| 时长 | 30 ~ 300 秒 |
| 可转写 | `CheckpointTranscriber` 状态 = `success`（非 `no_speech`） |

- 排序：`heat_score` 降序 → 时长降序 → video_id（确定性，便于测试）。
- 验收：
  1. 恰好产出 1 条 `script_replica`，或明确 `status = "not_found"` 并给出未满足的条件；
  2. 排序结果在相同输入下完全可复现；
  3. **脚本复刻视频不参与素材复用，因此豁免人脸门槛**（见 §7 Q9）。

#### P0-4 脚本思路提取（结构化脚本骨架）

- 功能：对选中的脚本复刻视频执行口播转写 → 生成结构化脚本骨架。
- 骨架字段：`hook`（0~5s 开场钩子）、`pain_or_context`（背景/痛点）、`product_reveal`（主体亮相）、`key_points[]`（要点，含 `start`/`end` 时间码 + 该段原文摘要）、`demo_or_compare`（演示/对比）、`conclusion`、`cta`。
- 分段规则：离线启发式（按 ASR 段落的时长与连接词切段），**不依赖 LLM**；LLM 增强为 P2 可选。
- 交付：`脚本思路.md`（人类可读，每段带时间码与"可借鉴点"）+ `脚本骨架.json` + `口播全文.txt` + `来源.json`（video_id/作者/URL/播放量/采集时间）。
- 验收：
  1. `脚本骨架.json` schema 校验通过，`key_points` ≥ 3；
  2. 所有时间码单调递增且 ≤ 视频时长；
  3. `脚本思路.md` 明确标注「抖音素材仅为发现与关注度证据，不得作为事实依据」；
  4. ASR 失败时降级：仍产出骨架（字段为空 + `warnings`），不得中断流程。

#### P0-5 「素材复刻视频」筛选（2~4 个）

- 筛选条件（须同时满足）：

| 条件 | 阈值 | 说明 |
| --- | --- | --- |
| 实拍画面充足 | 画面变化率 ≥ 阈值 **或** OCR 文本覆盖帧占比 ≤ 40% | P0 用轻量帧差代理；P1 升级为镜头边界切分 |
| 口播占比低 | ASR 字数/时长 < 1.2 字/秒（无口播视为 0） | 偏"画面演示"而非"讲解" |
| 时长 | 15 ~ 180 秒 | 单条素材可拼接 |
| 热度 | `heat_score` ≥ 候选池中位数 | 排除尾部噪声 |
| 作者去重 | 同一作者最多入选 1 条 | 满足"不同博主提供" |
| 人脸门槛 | 整片 `face_class ∈ {face_free, low_face}` | `face_heavy` 直接剔除 |

- 数量：目标 4 条，最少 2 条；不足 2 条时在清单标记 `insufficient=true` + 具体缺口原因。
- 验收：
  1. 入选 2~4 条，作者互不重复；
  2. 每条带 `选入理由`（命中哪些条件、各指标数值）；
  3. 候选池全为 `face_heavy` 时，返回 0 条 + 明确警告，而不是交付含人脸素材。

#### P0-6 人脸检测与「少人脸」排序

- **技术路线（硬约束）**：首选 `cv2.FaceDetectorYN`（YuNet，opencv 内置 ONNX 后端）→ 回退 `cv2.dnn` res10 SSD → 回退 Haar。三者均来自**已安装的 `opencv-python`**。
- **禁止**引入 `torch` / `insightface` / `mediapipe` / `dlib` 等新增原生依赖（本机 VC++ 运行库偏旧，曾导致 torch/ctranslate2 类原生库加载崩溃）。
- 采样参数（复用 `KeyframeOCR` 的 ffmpeg pattern，保证一致性与缓存复用）：

| 参数 | 值 |
| --- | --- |
| 采样频率 | `fps=1`（1 帧/秒） |
| 单视频上限 | `max_frames = 120` |
| 帧宽 | `scale='min(960,iw)':-2` |
| 计入人脸的框面积比 | ≥ 1.5%（低于此视为远处不可辨识，不计入） |

- 指标：

| 指标 | 定义 |
| --- | --- |
| `face_frame_ratio` | 命中人脸的采样帧数 / 采样帧总数 |
| `max_face_area_ratio` | 单帧最大人脸框面积 / 该帧面积 |
| `face_class` | `face_free`（≤ 5%）／`low_face`（5% < x ≤ 15%）／`face_heavy`（> 15%）／`unavailable` |

- 排序优先级：`face_free` > `low_face` > `face_heavy`（后者不进入交付）；同档内按 `heat_score` 降序。
- 验收：
  1. `face_heavy` 片段进入交付目录的数量 **= 0**；
  2. 交付的主素材片段 **100% 为 `face_free`**；
  3. `face_free`/`low_face` 分级在固定的合成测试帧上是确定性的；
  4. 人脸后端不可用时：`face_class = "unavailable"`、清单 `face_backend_status = "unavailable"`、`degraded = true`，**仍产出素材并给出警告，不得崩溃**；
  5. 人脸检测吞吐 ≥ 8 帧/秒（960 宽，单进程，CPU）；
  6. **只做人脸存在性检测，不做人脸识别/认出是谁**（见 §6）。

#### P0-7 批量下载与片段导出

- 功能：对入选的脚本复刻视频与素材复刻视频批量下载原片；对素材复刻视频按区间导出**片段级素材**（主素材 / 辅助素材）。
- 切片策略（P0）：主素材与辅助素材均以 **3~8 秒的无人脸区间**为单位导出；区间由人脸检测结果反推（连续 `face_free` 帧构成候选区间）。
- 导出方式：优先 ffmpeg 无损切片（`-c copy`）；ffmpeg 缺失时退化为「原片 + 区间清单」（`degraded = true` + 警告）。
- 复用：`resolve_media_tool(config, "ffmpeg")` 解析路径，**禁止硬编码**（当前本机 ffmpeg 位于项目外的 `D:\刘宇钊\codex_work\视频下载dy\.tools\ffmpeg\...`）。
- 验收：
  1. 每个片段有 `start`/`end` 时间码，与源视频可精确对齐（误差 ≤ 0.5s）；
  2. 每个片段独立可播放，且时长为 3~8 秒；
  3. 下载失败/超时的单条视频只影响自身，流程继续（逐条记录 `error`）；
  4. 单条视频下载 ≤ 90s，单主题下载总耗时 ≤ 8 min；
  5. 临时文件清理采用**逐文件 unlink + rmdir**，禁止 `shutil.rmtree`（沙箱拦截递归删除）。

#### P0-8 交付目录与清单规范

- 目录命名：`MM.DD<主题>复刻视频/`，例：`9.12苹果折叠屏复刻视频/`（月不补零、日两位；见 §7 Q6）。
- 主题需 sanitize（去 `\ / : * ? " < > |`），并保证全路径长度 < 260 字符。
- 目录树与清单字段见 §4。
- 验收：
  1. 一次运行只产出一个交付目录，重复运行产生新的一级目录或可 `--overwrite` 覆盖，绝不半覆盖；
  2. `清单.json` 通过 schema 校验，且包含 `evidence_disclaimer` 字段；
  3. 中文路径在 Windows 下创建/读写/播放均正常；
  4. 交付目录可整体拷贝到其他机器使用（不含绝对路径依赖）。

#### P0-9 CLI 子命令与可运行性

- 新增 CLI 路由（`src/douyin_intelligence/cli.py`）：
  - `material-replication run --theme "苹果折叠屏手机" [--business-date YYYY-MM-DD] [--pool-size N] [--dry-run]`
  - `material-replication inspect --folder "<交付目录>"`
  - `material-replication doctor`（探测 ffmpeg / 人脸后端 / ASR / OCR 可用性）
- 验收：
  1. `--help` 与 `doctor` 均能离线运行；
  2. `--dry-run` 只做候选池与打分，不下载、不切片；
  3. 新增 CLI 不改动 26 个既有子命令的行为；
  4. 同步新增/更新 `docs/tasks/` 任务指南与 `docs/handoff/`（含 `CODE_MAP.md`、`CURRENT_STATUS.md`、`PRODUCT_RULES.md`），并通过 `scripts/audit_handoff.py`。

### 3.2 P1（重要）

| # | 需求 | 验收标准 |
| --- | --- | --- |
| P1-1 | 镜头边界切分（轻量帧差/直方图）替代固定区间切片 | 片段与真实镜头边界对齐率 ≥ 70%；不引入新原生依赖 |
| P1-2 | 人脸检测结果缓存复用（按 video_id + 采样参数 key） | 重复运行命中缓存，人脸检测阶段耗时下降 ≥ 80% |
| P1-3 | OCR 辅助剔除「搬运/水印/片头版权条」画面 | 识别并标记 `has_watermark`；含明显水印片段默认降级为辅助素材 |
| P1-4 | 断点续跑与增量重跑 | 中断后重跑不重复下载已完成的视频；状态可见 |
| P1-5 | 并行下载与并行人脸检测（有界并发，默认 2） | 端到端耗时下降 ≥ 30%；并发上限可配置且不超 `max_concurrency_num` 约束 |
| P1-6 | 人类可读摘要 `00-交付说明.md` | 含主题、片段数、人脸分布、每个片段的建议用途、警告清单 |
| P1-7 | 片段自动标注「对应脚本骨架哪一段」 | 每个片段有 `suggested_use` 指向 `key_points[i]` 或 hook/demo/conclusion |

### 3.3 P2（可选）

| # | 需求 | 验收标准 |
| --- | --- | --- |
| P2-1 | Tkinter 工作台集成（主题输入 + 进度 + 结果打开） | 工作台可触发同一工作流，状态与 CLI 一致；不阻塞 UI |
| P2-2 | LLM 增强脚本骨架（受约束、默认关闭、可回退启发式） | 开关开时输出 schema 不变；LLM 不可用时自动回退且不报错 |
| P2-3 | 同一素材源的复用价值打分（历史沉淀） | 记录片段被采纳/废弃，形成本地评分；不影响 P0 排序可复现性 |
| P2-4 | 多主题批量模式（一次跑多个主题） | 每主题独立目录；单主题失败不影响其他主题 |

---

## 4. 输出物规范

### 4.1 交付目录树（示例：主题「苹果折叠屏手机」）

```
9.12苹果折叠屏复刻视频/
├── 00-交付说明.md                      # 人类可读摘要（P1-6），含人脸分布与警告
├── 01-脚本思路/
│   ├── 脚本思路.md                     # 分段脚本骨架 + 可借鉴点（带时间码）
│   ├── 脚本骨架.json                   # 结构化骨架（schema 见 4.2）
│   ├── 口播全文.txt                    # ASR 全文
│   └── 来源.json                       # video_id/作者/URL/播放量/采集时间
├── 02-主素材/
│   ├── 苹果折叠屏-主素材-01-折痕特写.mp4
│   ├── 苹果折叠屏-主素材-01-折痕特写.json   # 片段元数据（schema 见 4.3）
│   ├── 苹果折叠屏-主素材-02-开合演示.mp4
│   └── 苹果折叠屏-主素材-02-开合演示.json
├── 03-辅助素材/
│   ├── 苹果折叠屏-辅助素材-01-铰链结构.mp4
│   ├── 苹果折叠屏-辅助素材-01-铰链结构.json
│   ├── 苹果折叠屏-辅助素材-02-系统界面滑动.mp4
│   └── 苹果折叠屏-辅助素材-02-系统界面滑动.json
├── 04-原片/                            # 可配置保留/清理（见 §7 Q5）
│   ├── 何同学_折叠屏体验_7304xxxx.mp4
│   └── ...
├── 05-过程数据/                        # 可复现证据，便于 inspect 与重跑
│   ├── candidate_pool.json
│   ├── search_report.json
│   ├── face_metrics.json
│   ├── scoring.json
│   └── run_log.json
└── 清单.json                           # 交付主清单（schema 见 4.4）
```

片段命名规则：`<主题>-<角色>-<序号>-<用途标签>.mp4`
（角色 = `主素材` / `辅助素材`；序号两位；用途标签 ≤ 8 字中文、来自 `suggested_use`）

### 4.2 `脚本骨架.json`

```json
{
  "schema_version": 1,
  "video_id": "7304xxxxxxxxxxxxx",
  "author": "老师好我叫何同学",
  "source_url": "https://www.douyin.com/video/7304xxxxxxxxxxxxx",
  "duration_seconds": 168.4,
  "play_count": 8120000,
  "heat_score": 0.91,
  "asr_status": "success",
  "sections": {
    "hook":            { "start": 0.0,   "end": 4.8,  "summary": "悬念反问开场" },
    "pain_or_context": { "start": 4.8,   "end": 22.1, "summary": "现有折叠屏的顾虑" },
    "product_reveal":  { "start": 22.1,  "end": 41.0, "summary": "产品亮相" },
    "demo_or_compare": { "start": 96.0,  "end": 132.5, "summary": "与上一代对比" },
    "conclusion":      { "start": 132.5, "end": 158.0, "summary": "结论" },
    "cta":             { "start": 158.0, "end": 168.4, "summary": "互动引导" }
  },
  "key_points": [
    { "index": 1, "start": 41.0, "end": 62.3, "summary": "铰链结构", "borrowable": "用结构件特写建立专业感" }
  ],
  "warnings": []
}
```

### 4.3 片段元数据 `<片段名>.json`

```json
{
  "schema_version": 1,
  "clip_id": "main-01",
  "role": "main",
  "file": "苹果折叠屏-主素材-01-折痕特写.mp4",
  "source": {
    "video_id": "7305xxxxxxxxxxxxx",
    "author": "博主B",
    "source_url": "https://www.douyin.com/video/7305xxxxxxxxxxxxx",
    "play_count": 420000,
    "heat_score": 0.63,
    "folder": "04-原片/博主B_折叠屏开箱_7305xxxxxxxxxxxxx.mp4"
  },
  "timecode": { "start": 12.4, "end": 18.9, "duration": 6.5 },
  "media": { "width": 1080, "height": 1920, "fps": 30, "has_audio": true },
  "face": {
    "backend": "opencv_yunet",
    "status": "ok",
    "face_frame_ratio": 0.0,
    "max_face_area_ratio": 0.0,
    "face_class": "face_free",
    "sampled_frames": 64
  },
  "suggested_use": "key_points[1] 铰链结构",
  "warnings": []
}
```

### 4.4 `清单.json`

```json
{
  "schema_version": 1,
  "theme": "苹果折叠屏手机",
  "folder": "9.12苹果折叠屏复刻视频",
  "business_date": "2026-09-12",
  "generated_at": "2026-09-12T21:40:00+08:00",
  "keywords_used": ["苹果折叠屏手机", "折叠屏 折痕", "iPhone Fold"],
  "candidate_pool_size": 80,
  "script_replica": {
    "status": "found",
    "video_id": "7304xxxxxxxxxxxxx",
    "author": "老师好我叫何同学",
    "heat_score": 0.91,
    "skeleton": "01-脚本思路/脚本骨架.json",
    "script_notes": "01-脚本思路/脚本思路.md"
  },
  "material_replica_sources": [
    { "video_id": "7305xxxxxxxxxxxxx", "author": "博主B", "face_class": "face_free", "selected_reason": "画面变化率高/口播密度0.2字每秒" }
  ],
  "main_materials":     [ { "clip_id": "main-01", "file": "02-主素材/...", "duration": 6.5, "face_class": "face_free", "suggested_use": "key_points[1] 铰链结构" } ],
  "supporting_materials":[ { "clip_id": "support-01", "file": "03-辅助素材/...", "duration": 5.2, "face_class": "face_free", "suggested_use": "hook" } ],
  "counters": {
    "candidates": 80, "downloaded": 6, "face_checked": 6,
    "clips_exported": 6, "clips_rejected_face_heavy": 3, "clips_rejected_duration": 2
  },
  "face_backend": "opencv_yunet",
  "face_backend_status": "ok",
  "ffmpeg_status": "ok",
  "degraded": false,
  "insufficient": false,
  "warnings": [],
  "evidence_disclaimer": "抖音素材仅为发现与关注度证据，不得作为事实依据；人脸指标为自动检测结果，交付前需人工复核。"
}
```

---

## 5. 非功能需求

### 5.1 性能（可量化）

| 阶段 | 目标 | 硬上限 |
| --- | --- | --- |
| 候选池采集（80 条候选） | ≤ 8 min | 12 min |
| 批量下载（≤ 6 条视频） | ≤ 8 min | 12 min |
| ASR 转写（1 条 ≤ 300s 视频） | ≤ 3 min | 5 min |
| 人脸检测（≤ 6 条 × ≤ 120 帧） | ≤ 6 min（≥ 8 帧/秒） | 10 min |
| 片段切片（≤ 12 个片段） | ≤ 3 min | 6 min |
| **单主题端到端** | **≤ 30 min** | **45 min** |

- 任一步骤超时只降级该步骤（`partial` + 警告），不终止全流程。
- 内存峰值 ≤ 2 GB；人脸检测单帧分辨率不超过 960 宽。

### 5.2 Windows 兼容（硬约束）

| 约束 | 要求 |
| --- | --- |
| VC++ 运行库偏旧 | **禁止引入 torch / insightface / mediapipe / dlib 等新增原生依赖**；人脸检测只用已安装的 `opencv-python` 路径 |
| 依赖声明 | `opencv-python`、`onnxruntime` 当前**仅为传递依赖**，必须**显式写入 `pyproject.toml`**，避免上游变更导致断裂 |
| 后端可用性 | `doctor` 必须做运行时自检（人脸后端/ffmpeg/ASR/OCR），失败时降级不崩溃 |
| ffmpeg | 必须经 `resolve_media_tool()` 解析，禁止硬编码路径（本机 ffmpeg 在项目外目录） |
| 沙箱拦截递归删除 | 清理只用逐文件 `unlink` + `rmdir`，**禁止 `shutil.rmtree`**，沿用 `media_processing.py` 模式 |
| 路径 | 中文主题需 sanitize；全路径 < 260 字符；UTF-8 读写显式声明 |
| PATH | 不在 PATH 中查找 Python/Node；一律 `resolve_path(config[...])` → `.venv\Scripts\python.exe` |
| 进程安全 | 不使用 `os.kill(pid, 0)` 探测 PID；不使用信号探测（见 `RUNTIME_SAFETY.md`） |

### 5.3 安全与合规边界

| 边界 | 要求 |
| --- | --- |
| 凭据 | 不读取/复制/提交 Cookie、API Key、浏览器用户目录、`.env.local` |
| 证据分级 | 抖音素材仅作**发现与关注度证据**，不得作为事实依据；清单必须含 `evidence_disclaimer` |
| 输出内容 | 交付物不得把素材画面内容表述为已核实事实 |
| 上游隔离 | 不修改 `third_party/MediaCrawler` |
| 素材存储 | 下载素材与交付目录不入版本库（`.gitignore`） |
| 有界执行 | 所有网络/浏览器/模型/子进程操作必须有有限预算与超时 |
| 人脸数据 | 只产存在性指标（比例/面积），**不产人脸特征向量、不存人脸截图、不识别身份** |

### 5.4 可测试性

- 新增 ≥ 20 个离线单测，覆盖：关键词扩展、候选池归一化、三类筛选排序、人脸分级（合成帧 fixture）、片段区间反推、目录与清单 schema、降级路径、CLI dry-run。
- 测试**禁止**真实浏览器、真实下载、真实模型调用；使用 fixture 视频/合成图像。
- 全量 pytest 由 296~298 保持全绿并增至 **≥ 316**，既有用例语义不得改动。
- 排序与分级必须是**确定性函数**，同输入同输出。
- `compileall` 通过；`scripts/audit_handoff.py` 通过。

---

## 6. 范围外（Explicit Non-Goals）

| 不做 | 说明 |
| --- | --- |
| 不做视频拼接 / 剪映草稿生成 | 用户明确：怎么拼接是下一步的事 |
| 不做 TTS 配音、字幕烧录、成片渲染 | 属于下游生产环节 |
| 不做平台发布 / 上传 / 定时投放 | 不在本项目职责内 |
| 不做素材源扩展 | 本期不接 Pexels/其他源，只解决抖音侧供应 |
| 不做人脸识别 | 只做「有没有脸」的存在性检测，不做认人/明星识别/人脸聚类 |
| 不做版权判定与授权谈判 | 只提供来源信息（作者/URL/video_id）供用户自行判断 |
| 不做风控规避 | 不绕过登录、验证码、频控；采集沿用有界预算 |
| 不做历史存量素材回溯迁移 | 本期只服务新工作流 |
| 不改动现有 26 个 CLI 子命令语义 | 新增子命令，不重构既有行为 |
| 不修改 `third_party/MediaCrawler` | 上游代码保持原样 |

---

## 7. 待确认问题（需用户拍板）

| # | 问题 | 建议默认值 | 影响 |
| --- | --- | --- | --- |
| Q1 | 单主题候选池目标规模与采集预算上限？ | 目标 80，上限 120 | 直接影响耗时与准确度 |
| Q2 | 人脸阈值是否采用 `face_free ≤ 5%` / `low_face ≤ 15%` / 计入面积比 ≥ 1.5%？ | 采用上述值 | 决定交付素材数量能否达标 |
| Q3 | 「手部 / 背影 / 低头不露脸」是否允许？侧脸是否算「脸」？ | 手部、背影、后脑勺允许；侧脸按检测命中处理（偏保守） | 直接影响可用素材量 |
| Q4 | 主素材是否必须 `face_free`（一票否决），还是允许 `low_face` 降级交付并标警告？ | 主素材必须 `face_free`；`low_face` 仅可进辅助素材 | 与「少人脸」优先级定义强相关 |
| Q5 | 切片粒度：3~8 秒固定区间是否合适？是否需要保留原片（`04-原片/`）？ | 3~8 秒；保留原片，可配置清理 | 影响交付体积与后续拼接自由度 |
| Q6 | 目录命名：月份是否补零（`9.12` vs `09.12`）？主题长度上限？ | `9.12` 形式（月不补零、日两位）；主题 ≤ 12 字 | 影响目录规范与自动化 |
| Q7 | 每条素材复刻视频产出几个片段？总片段数上限？ | 每条 1~2 个；总片段 ≤ 12 | 影响耗时与可用性 |
| Q8 | ffmpeg 缺失时的降级（原片 + 区间清单，不实际切片）是否可接受？ | 可接受，标记 `degraded = true` | 影响交付形态 |
| Q9 | 脚本复刻视频是否需要满足「少人脸」？ | 豁免（它只提供脚本思路，不复用其画面） | 若非豁免，高播放讲解类视频几乎全部不合格 |
| Q10 | 准确度验收是否需要人工抽检？谁来做？ | 首期由用户人工抽检 1 个主题，产出可用率数据 | 决定「准确度 ≥ 60%/50%」能否被验证 |

---

## 8. 附：与现有资产的复用关系（供架构阶段参考）

| 需求 | 复用现有资产 | 新增 |
| --- | --- | --- |
| P0-2 采集 | `search_collector.collect_search()` / `controlled_keywords()` / `mediacrawler_runner.py` / `artifact_safety.sanitize_raw_files()` | 主题→关键词扩展；候选归一化 |
| P0-4 脚本骨架 | `media_processing.CheckpointTranscriber`（含分段缓存） | 骨架结构化切分 |
| P0-5 画面代理指标 | `media_processing.KeyframeOCR` 的 ffmpeg 抽帧 pattern | 帧差/覆盖度计算 |
| P0-6 人脸 | 已装 `opencv-python` 5.0.0.93、`resolve_media_tool()` | `face_metrics` 模块（YuNet/dnn/Haar + 分级） |
| P0-7 下载/切片 | `materials.py` / `material_pipeline.py` / `media_tools.py` | 批量下载编排、区间切片、清理 |
| P0-8 交付 | `exporter.atomic_write_json()`、`daily_material_pack.py` 的清单思路 | `MM.DD<主题>复刻视频/` 目录规范与清单 schema |
| P0-9 CLI/文档 | `cli.py` 路由、`doctor.py`、`docs/tasks/` + `docs/handoff/` 工作流 | 3 个新子命令 + 任务指南 + handoff 更新 |
