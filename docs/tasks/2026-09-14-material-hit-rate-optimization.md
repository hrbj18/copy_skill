# 单次开发指导文档：素材命中率改造（2026-09-14）

> 本文档是本次优化的**唯一执行依据**。动手前写，动手后逐条对照验收。
> 作者：team lead ｜ 状态：已定稿，开执行

---

## 一、本次开发目标

### 1.1 一句话目标

把「素材命中率」从**名义存在**变成**真实生效**——让每一次交付的源片，其标题/画面确实指向该主题的产品；并把今天 9.14 除点名 4 期之外的 **7 期**素材按新口径重新获取。

### 1.2 点名不动的 4 期

`9.14华为昇腾950DT涨价` / `9.14华为Mate XT2 非凡大师` / `9.14特斯拉 Cybercab 无人驾驶` / `9.14大疆 Osmo Pocket 4 Pro`
—— **不重跑、不修改、不纳入验收**。

### 1.3 本次重跑的 7 期

| # | 主题 | 已知问题（来自核验） |
|---|---|---|
| 1 | Microduck 机械鸭机器人 | 0/3 命中；池内其实 52% 标题含 microduck |
| 2 | iRobot Roomba 875 扫地机器人 | 1/4 命中 |
| 3 | 充电宝3C认证新规 | 63% 体积来自 ≥163 天旧片 |
| 4 | 内存涨价 最贵装机季 | 79% 体积来自 7 个月前旧片 |
| 5 | 显卡涨价 RTX5090 | 3/8 命中；含 2025-01 的 5090D 开箱 |
| 6 | 苹果 iPhone Duo 折叠屏 | 待核 |
| 7 | DeepSeek V4.1 Flash | 待核 |

### 1.4 用户已确认的关键口径（不可偏离）

1. **命中判定 = 标题命中 且 内容确实是该产品**。
2. **视觉确认必须全自动，不允许有人工确认环节**。
3. **主题由我方落到具体品牌/型号**（"题目都是你自己给的，你应该知道对应哪个型号"）。
4. **准优先，宁可体积不足**（70 MB 不再凌驾于相关性之上）。
5. **不达标时自动重试**（换词 / 换源 / 收窄池子）。

---

## 二、问题根因（改造的依据，均已在阶段一调研中证实）

### 2.1 机械鸭 0 命中的四层叠加

| 层 | 事实 | 证据 |
|---|---|---|
| 措辞层 | 抖音实际写「**机器鸭**」，我们搜「机械鸭」；标题含 `microduck` **22/42 = 52%**，含「机械鸭」**0 条** | 候选池全量标题 |
| 召回层 | 9 个词里 **4 个零召回**（交互/评测/上手/新品） | `search_report.json` |
| 排序层 | `relevance` 恒 0（整串匹配）⇒ 退化成纯热度 ⇒ 入选前 6 名全是泛词 | `download_budget.json` |
| 闸门层 | 下载前 5 道闸门，**无一道判相关性** | `replication_selection.py:1479-1564` |

### 2.2 三处机制缺陷（代码级）

| 缺陷 | 位置 | 后果 |
|---|---|---|
| `category_attribute` lane 产出**无主题限定**的类目词（`机器人 演示`） | `replication_theme.py:108-115` | 泛词灌池、霸榜 |
| `relevance` 只作**排序键**、无阈值 | `replication_selection.py:1432` | 相关度 0 的候选照样下载 |
| 顶层 `degraded` 与 `relevance.degraded` **完全独立** | `replication_pipeline.py:821` vs `:561` | 假达标对上层不可见 |

### 2.3 已发现但属于「同族」的问题（本轮一并收口）

- 素材层**时效超期**（显卡期最旧 601 天、充电宝期中位 163 天）——`publish_time_type=0` 的副作用。
- **跨期素材复用**（`U技_Unitree G1` 在 Microduck 与 Roomba 期各出现一次，字节数相同）。
- 体积核算**未剔除不相关片**。
- `清单.json` 的 `material_replica_sources[]` **缺 `published_at` / `title` / `bytes` / `source`**，用户无法自检。

---

## 三、任务拆解

| 编号 | 任务 | 主改动文件 | 依赖 |
|---|---|---|---|
| **T1** | 关键词主体化 + 别名扩张 + 泛词带限定 | `replication_theme.py` | 无 |
| **T1b** | **无空格/混排主题的整串词条空洞**（P0，见 §四） | `replication_theme.py` | T1（同 owner） |
| **T2** | 相关性由「排序键」升级为「准入闸门」 | `replication_selection.py` | T1 |
| **T3** | 命中率阈值 + `degraded` 传导顶层 | `replication_selection.py`、`replication_pipeline.py` | T2 |
| **T4** | 全自动视觉确认（抽帧 + OCR + 主体比对） | 新增 `replication_visual.py` | T2 |
| **T5** | 多源接线（B站补海外小众题材） | `replication_candidates.py`、`sources/` | 需真实可用性验证 |
| **T6** | 素材层时效闸门 + 跨期去重 + 清单补字段 | `replication_selection.py`、`replication_pipeline.py` | T2 |
| **T7** | 7 期重跑与验收 | 配置 + 批次脚本 | T1~T6 |

**优先级**：T1 → T2 → T3 是**病根**，必做且必须最先做（做完就能验证「机械鸭这类题材还 0 不 0 命中」）。
**T1b 是开闸前的硬阻断**（不开则两期归零，见 §四）。T4 是用户明确要求。T5 视 B站适配器真实验证结果决定。T6 收口。

---

## 四、实现思路与具体改动

### T1 · 关键词主体化（`replication_theme.py`）

**问题**：现有关键词是「整串 + 后缀」（`Microduck 机械鸭机器人 开箱`）与「无限定类目词」（`机器人 演示`）两类，前者永远匹配不上标题，后者引狼入室。

**改法**：

1. 新增 `_SUBJECT_ALIASES`：产品级别名词典（键为中文常用写法，值为别名元组），
   例：`"机械鸭": ("机器鸭", "Microduck", "机械鸭子")`，`"扫地机器人": ("扫地机",)` 等。
   同时支持从 `config.jobs.material_replication.subject_aliases` **覆盖/扩展**（缺省时完全等价）。
2. 新增 `_subject_head(base)`：从主题串中剥离品类词与 intent 后缀，得到**核心主体**。
   例：`Microduck 机械鸭机器人` → `Microduck 机械鸭`。
3. `_category_attribute_terms` 的产物**必须带主体限定**：`f"{head} {attribute}"` 而非 `f"{category} {attribute}"`。
4. `intent` lane 由 `f"{base} {suffix}"` 改为 `f"{head} {suffix}"`（用主体，不用整串）。
5. 新增 `alias` 扩展：`_SUBJECT_ALIASES` 命中时把**别名单独**作为关键词（`机器鸭` 独立成词，而不是只做替换）。
6. **顺序不变**：`_LANE_CYCLE` 保持；`head` 与 `base` 相同时行为与改动前**逐字节一致**。

**验收点**：`expand_keywords("Microduck 机械鸭机器人", cfg)` 中不再出现 `机器人 演示`/`机器人 交互`，
且出现 `机器鸭`、`Microduck 机械鸭` 这类短精确词。

### T1b · 无空格 / 混排主题的「整串词条」空洞（P0，开闸前必须修）

**发现方式（可复现）**：`.tmp/predict_gate_0914.py` —— 拿 9.14 已落盘的 11 个
`candidate_pool.json`，离线跑 `subject_terms` + `subject_hit_count`，**不联网、不写盘**。

**问题**：`replication_theme.py:423` 的 `for token in head.split(): add(token)` —— 主题**没有空格**时
`head` 就是整串，词表里只剩一个整串词条。实测：

| 期 | 现状 `subject_terms` | 闸门准入 |
|---|---|---|
| **充电宝3C认证新规** | `['充电宝3C认证新规']` | **0 / 31** |
| **华为昇腾950DT涨价** | `['华为昇腾950DT涨价']` | **0 / 17** |

`term_hits_title` 对单 token 走整串子串匹配 ⇒ 没有标题含这 9 个连续字符 ⇒ 准入恒 0
⇒ `relevance_gate.enabled=true` 一开，**这两期直接 0 条素材、整期失败**。
带空格的主题（`内存涨价 最贵装机季`）恰好被 `.split()` 救了一命，所以这个 bug 长期未暴露。

**改法**：`subject_terms` 里对每个已 add 的 token **追加**按「CJK ↔ 非 CJK 脚本边界」切出的片段。四条设计约束：

1. **只做加法**（整串 token 永远保留）⇒ **召回单调不减**，最坏只是多召回，绝不会更少。
2. **单脚本 token 不切**（`苹果折叠屏` 全 CJK、`RTX5090` 全非 CJK）⇒ 保证其余 9 期词表**逐字节不变**。
3. **事件词过滤**：纯事件/状态词零产品辨识度，不许单独成词条（否则 `涨价` 一词放行所有涨价视频）。
4. **全有或全无**：只要有一段「具体性」不达标，就**整条 token 的切分全部放弃**。
   反例 `华为Mate` → `华为` + `Mate`，而 `华为` 是赤条条的品牌名（会放行所有华为视频）；
   收紧后该期准入从虚高的 97.2% **逐字节回到 93.1%**。「具体性」= 含数字/拉丁字母，或纯 CJK 段 ≥3 字。

**实测效果（T1b 后）**：9 期词表逐字节不变；充电宝 `0/31 → 30/31`（新增 30 条、其中 ≥60s 长片 24 条、
合计 6100s）；昇腾 `0/17 → 9/17`（新增 9 条、5 条长片）。⇒ A5 为标准。

**明确不做**：`expand_keywords:364` 的 `add(head)` 有**同族缺陷**（多词 head 作为单一整短语词条，
AND 语义下永不命中 ⇒ 两期 `live_count == 0` ⇒ `degraded == true`）。**本期不动**：
改它要动 `_LANE_CYCLE` 的词条分配，会让 11 期的关键词快照全变、波及搜索行为，
风险远大于收益，且**不阻塞用户目标**（选材由闸门保证；`degraded` 只是诊断）。
按 B4 修订后的口径，只要求它在 `warnings` 里说清原因。**留作下一期独立任务。**

### T2 · 相关性闸门（`replication_selection.py`）

**改法**：

1. `relevance_report` 增加两个字段（**additive**，不破坏现有消费方）：
   - `subject_terms`：从 `live_terms` 中筛出的「含主体 token」的词（按 T1 的 `head`/别名判定）；
   - `hit_ratio`：`命中主体词的候选数 / 候选总数`。
2. 下载循环（`for index, candidate in enumerate(pool)`，`:1479`）在 `is_video_candidate` 之后、`author_duplicate` 之前插入**相关性闸门**：
   - 条件：`candidate.video_id not in subject_hit_ids` 且闸门开启；
   - 动作：`unmet.append({stage: "relevance", reason: ...})` 并 `continue`；
   - 新增计数器 `rejected_relevance`。
3. **开关**：`config.jobs.material_replication.relevance_gate`（`{enabled: bool, min_subject_hits: int}`）。
   **缺省 `enabled=False` ⇒ 与改动前逐字节等价**（硬约束）。
   本次运行的 config 里**显式打开**。

### T3 · 命中率阈值 + degraded 传导

1. `relevance_report.degraded` 的判据由 `live_count == 0` 放宽为
   `live_count == 0 or hit_ratio < subject_hit_threshold`（阈值可配，缺省 0 ⇒ 等价于旧行为）。
2. `replication_pipeline.py`：最终 `degraded` 需要 **OR 上 `relevance_detail["degraded"]`**，
   使「题材相关度无法区分候选池」在顶层可见；warning 文案同步说明触发原因（是「无词命中」还是「命中率过低」）。
3. `run_log.json` 的 `search_attribution.relevance` 增加 `hit_ratio`、`subject_terms`、`subject_hits` 字段。

### T4 · 全自动视觉确认（新增 `replication_visual.py`）

**依据**：用户要求「用上视觉确认，看看视频封面之类的」，且「不要有人工确认」。

**本机能力**：ffmpeg ✅、RapidOCR（3 个 onnx）✅、**无 VLM / 无 torch / 无 transformers** ❌。

**设计（下载后确认，不引入新依赖）**：

1. 对每条已下载的源片，用 ffmpeg 抽取 **3 帧**（首帧 + 1/3 处 + 2/3 处），落盘为临时 jpg。
2. 用已有的 OCR 能力（复用 `visual_ocr` / `RapidOCR`）对每帧做文字识别。
3. 把「**文件名 + 全部帧 OCR 文本**」拼成 `visual_text`，与 T1 的**主体 token 集**比对。
   > **⚠️ 设计修正（实现期定稿，2026-09-14）**：初稿写的是「**标题** + 全部帧 OCR 文本」，**已废弃**。
   > 理由：标题**已经被 relevance_gate 消费过**，再把它喂进视觉确认，这一项就**丧失了独立性** ——
   > 于是恰好发现不了我们最想抓的那个病：**「元数据对、画面错」**（经典样本见 B7）。
   > 只吃「文件名 + OCR 文本」才是**独立证据通道**；且生产路径里文件名就是 `<video_id>.mp4`，
   > 等于"id + OCR"，比含标题更干净。此修正由实现者提出并被采纳，冒烟用例 ①②③ 证实：
   > **即使标题在场，含标题与不含标题给出的判定仍然正确**，说明不含标题没有牺牲判别力。
4. 产出 `visual_verify.json`：逐条记录 `video_id / frames / ocr_text / subject_hits / verdict`。
5. **判定**：`verdict = "hit"` 当且仅当 `subject_hits > 0`；`conclusive = 至少一条 hit`。
   > **⚠️ 设计修正（实现期定稿）**：初稿写的是「`miss` 的条目**从交付中原片/清单剔除**」，**已废弃**。
   > 理由：视觉确认是**佐证**不是**闸门** —— **误杀比漏放更贵**（一个产品名只出现在画面无文字处的
   > 真素材，会因为 OCR 抓不到而被扔掉）。定稿语义：**`conclusive=False` 不剔除任何素材、不计入顶层
   > `degraded`**，只 ① 追加一条 warning ② 在 `清单.json` 记摘要。剔除类决策一律交给
   > relevance_gate（它判的是**标题**，是可判定的）。
6. **兜底**：文件不存在 / ffmpeg 失败 / 0 帧 / OCR 异常 → `verdict="unknown"`、`frames=0`、**绝不抛异常**；
   全 miss 或 unknown → `conclusive=False`，按第 5 条处理（标注，不静默、不误杀）。
7. **无任何人工确认路径**（用户硬要求）：不产出联系表、不产出待确认清单、不需要人点确认。
   临时目录用 `tempfile.mkdtemp(prefix="visual-verify-")` + `try/finally shutil.rmtree(ignore_errors=True)`。

**为什么不做「下载前」视觉确认**：候选记录里**没有封面 URL 字段**（jsonr 字段核查过），
下载前拿不到图；强行下载封面等于把「下载」提前，成本不降反升。

### T5 · 多源接线（条件任务）

`src/douyin_intelligence/sources/` 已注册 `douyin` / `ytdlp` / `bilibili` 三个适配器，
`base.py` 契约齐备，**但生产路径 `get_source` 零调用**（抖音在 `replication_candidates.py:359-371` 硬接）。

**改法**：
1. `Candidate` 增加 `source: str = "douyin"` 字段；
2. `collect_candidate_pool` 改为按 `settings["sources"]` 遍历（缺省仍只用 douyin，保持等价），
   把各源的 `SourceResult.candidates` **归并**进同一池；
3. 归并时按 `(source, video_id)` 去重；
4. 下载阶段用 `CompositeMediaResolver` 派发。

**前置门禁**：先用 B站适配器跑**一次真实搜索**（例如搜 `机器鸭`），确认能返回真实候选；
**跑不通则本任务降级为「只接线、不启用」**，并在文档中记录实测结论。

### T6 · 时效闸门 / 跨期去重 / 清单补字段

1. **时效闸门**：`candidate_pool.json` 已带 `published_at`（ISO 字符串），
   在选材阶段按 `jobs.material_replication.material.max_age_days` 过滤（缺省 0 = 不过滤）。
   本次运行按题材类别设定：行情/政策类 7 天，产品类按窗口。
2. **跨期去重**：新增 `data/cache/material-replication/dedup_index.json`，
   记录已交付过的 `video_id`；选材时命中即跳过（`stage="cross_run_duplicate"`），
   交付后回写。开关 `jobs.material_replication.dedup_across_runs`（缺省 false）。
3. **清单补字段**：`material_replica_sources[]` 增加
   `title / bytes / published_at / source / hit_ratio / visual_verdict`。

---

## 五、前期准备（已完成）

| 项 | 状态 |
|---|---|
| 三路只读调研（根因 / 闸门 / 多源架构） | ✅ 已完成，证据见本文件第二节 |
| 关键代码通读（`replication_theme.py` 全量、`replication_candidates.py` 全量、`sources/__init__.py` 全量、selection/pipeline 关键段） | ✅ |
| 基线测试数字确认 | ✅ 裸跑 `pytest tests` = **744 passed** |
| 环境确认 | ✅ venv `copy_skill-main\.venv\Scripts\python.exe`；Bash 需 `export PATH="/usr/bin:/bin:$PATH"`；pytest 需 `env -u PYTHONPATH` |
| 反向约束确认 | ✅ **禁止并发跑 pytest**；禁 `git stash/gc/repack`；含转义正则先落盘再执行 |

---

## 六、开发流程步骤

1. **T1**（theme）→ 跑 `pytest tests/test_replication_theme.py`（若有）与全量回归。
2. **T2 + T3**（selection / pipeline）→ 定向测试 + 全量回归。
3. **T4**（视觉确认，新模块）→ 新单测 + 真实抽帧 OCR 冒烟。
4. **T5**（多源，条件）→ 先真实验证 B站适配器，再接线。
5. **T6**（时效 / 去重 / 清单）→ 定向测试 + 全量回归。
6. **配置**：把新开关写进 `config/content_intelligence.json`（显式开启）。
7. **T7 重跑**：7 期串行，每期之间 120s 限流间隔，由主 agent 用后台任务亲自启动
   （单 worker 会话寿命约 15 分钟，撑不住 ~90 分钟的批次）。
8. **验收**：逐条对照第七节。

**每个实现步骤的硬性要求**：
- 改完必须跑全量测试，且**裸跑**（不加 `--ignore`）；
- 跑完 `git ls-files --deleted` 必须为空；
- 每个逻辑改动单独提交，提交信息说明「改了什么 / 为什么」。

---

## 七、验收标准（本次是否达成的判据）

### A. 回归与正确性

| # | 标准 | 判定方式 |
|---|---|---|
| A1 | 全量测试 **0 failed**（基线：开工时 744 passed；T6 落地前实测 **808 passed / 0 failed / EXIT=0**）。<br>⚠️ 本仓 pytest **不打印 summary 行**，用 `EXIT` 码 + 点行判断，别去找 "passed" 字样。 | `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest tests -q` 裸跑 |
| A2 | 跑完 `git ls-files --deleted` 为空 | 命令输出为空 |
| A3 | 新增逻辑均有单测覆盖（theme 主体化 / 相关性闸门 / 命中率阈值 / degraded 传导 / 视觉判定 / 时效闸门 / 跨期去重） | 新增测试文件存在且通过 |
| A4 | **配置键缺省时行为与改动前等价** | 现有守卫用例 + 新增等价性用例通过 |
| A5 | **11 个 9.14 历史候选池的离线门禁回归**（`.tmp/predict_gate_0914.py`）：<br>T1b 后 9 期 `subject_terms` **逐字节不变**、2 期空洞被填（充电宝 `0/31 → 30/31`、昇腾 `0/17 → 9/17`） | 重跑该脚本，比对输出 |
| A6 | **测试不得与生产配置耦合**：`load_config()` 在测试内必须剥离 opt-in 开关（`relevance_gate` / `visual_verify`），需要它们的用例显式 opt in。 | `tests/conftest.py` 的 seam + 其守卫用例 |

### B. 命中率（核心）

| # | 标准 | 判定方式 |
|---|---|---|
| B1 | Microduck 主题重跑后，`search_attribution.relevance.live_terms` **非空且包含主体词** | 读 `run_log.json` |
| B2 | Microduck 重跑后，`04-原片` 中**标题含主体词（microduck/机器鸭/机械鸭）的比例 ≥ 80%** | 逐条核对文件名与 `清单.json` |
| B3 | 7 期**全部**满足：入选原片「标题含主体词」比例 **≥ 80%** | 同上，逐期 |
| B4 | 7 期**全部**满足：`relevance.degraded` **自洽且可诊断**。<br>`degraded == true` ⇒ `warnings` 里必须有一条说明原因（`live_count == 0` 或 `hit_ratio < min_hit_ratio`）；<br>`degraded == false` ⇒ 必须 `live_count > 0`。 | 读 `run_log.json` |
| B4b | 7 期**全部**满足：「闸门准入集」非空，且准入集内每条素材标题命中主体词（`min_subject_hits >= 1` 的直接推论）。<br>**准入集为空 ⇒ 该期不通过**（这是 T1b 要堵的洞）。 | 由 `candidate_pool.json` + `subject_terms` 离线复算，或读 `run_log` 的 `rejected_relevance` |
| B5 | 7 期**全部**满足：顶层 `degraded` 与 `relevance.degraded` 一致（不再出现假达标） | 读 `run_log.json` |
| **B7** | **「已知跑偏样本」不得复现**（比命中率更能证伪）：<br>· iRobot Roomba 875 期 `04-原片` **不得**出现 `1X Neo` / `Unitree G1`<br>· Microduck 机械鸭期 **不得**出现 `Unitree G1` | 逐条核对片名 |

> **B7 的由来（本轮最有价值的第三方发现）**：视觉确认模块的实现者在对 **Roomba 期已交付原片**跑抽帧 + OCR 时发现，
> 交付里的两条源片其实是 **1X Neo 人形机器人手** 与 **Unitree G1 人形机器人**，**而该期结论是「命中」、
> `relevance.degraded = false`、warnings 为空** —— 即「**做扫地机器人的一期，交付了人形机器人的片**」。
> 这条样本的价值在于它**同时绕过**了 T3a 的降级传导与 T2 的主体闸门，是「素材跑偏却一路绿灯」的活标本。
> **而它本可以被拦住**：两条片名都不含 `iRobot` / `Roomba` / `875` / `扫地机器人` / `扫地机`，
> 主体闸门会直接拒掉。之所以没拦，是因为闸门当时**还没接线** ——
> `replication_pipeline.py:959` 缺 `theme=theme`，导致 `:1505` 的 `bool(theme)` 恒假、整条闸门是死代码。
> ⇒ 该接线已由主 agent 补上（`5f7ae62`），B7 就是它的**端到端可判定验收**。

> **B4 的口径修订说明（09-14，我改了自己的标准）**：初稿写的是「7 期 `degraded` 全为 false」，**这条是错的**，理由有二：
> 1. `degraded` 的原始语义是「relevance **打分**无法区分这批池子」（`live_count == 0` ⇒ 所有分恒 0），它是**打分轴**的诚实告警，不是「内容不相关」的判据。历史实测已证：内存涨价期 18 条 relevance 全 0，但交付片名 **7/7 全对** —— 这是**假阴性**，不是真缺陷。
> 2. 强行要求全 false 会诱使实现去改 `live_count == 0` 的语义，那会破坏「配置键缺省时逐字节等价」的硬约束，属无证据的范围扩大。
> ⇒ 因此 B4 改为**自洽性**判据（告警必须说的出原因），把「内容是否相关」交给 **B3（标题命中率）+ B4b（准入集非空）+ D（视觉确认）** 三处证据。
> 已知：`充电宝3C认证新规` / `内存涨价 最贵装机季` 两期会因 `live_count == 0` 而 `degraded == true`（根因见 §四 T1b 备注：`expand_keywords` 把多词 head 作为**单一整短语**词条，AND 语义下永不命中）。**本期不动这个语义**，只要求它在 `warnings` 里说清原因。

### C. 交付质量

| # | 标准 | 判定方式 |
|---|---|---|
| C1 | 7 期 `04-原片` 中，**跨期重复的 video_id 数量 = 0** | 汇总各期清单比对 |
| C2 | 7 期**全部**满足：原片 `published_at` 中位年龄在题材窗口内（行情/政策类 ≤ 14 天，产品类 ≤ 90 天） | 由 `published_at` 反算 |
| C3 | `清单.json` 的 `material_replica_sources[]` 每条含 `title / bytes / published_at / source / hit_ratio / visual_verdict` | 读清单 |
| C4 | 7 期交付目录结构完整（7 项齐全） | 列目录 |
| C5 | 交付体积落在 70~150 MB；**若因「准优先」不足 70 MB，必须在交付说明中显式标注 `insufficient_bytes` 及原因** | 体积统计 + 交付说明 |

### D. 视觉确认

| # | 标准 | 判定方式 |
|---|---|---|
| D1 | 7 期均产出 `05-过程数据/visual_verify.json`，逐条含 `frames / ocr_text / subject_hits / verdict` | 文件存在且字段完整 |
| D2 | **全流程无任何人工确认步骤**（代码里不存在等待人工输入的路径） | 代码审查 |
| D3 | 若某期判定为 `inconclusive`，必须在交付说明中标注 | 读交付说明 |

### E. 失败即算不通过

- 出现任一 `status=failed`；
- 出现「标题含主体词比例 < 80%」却不带 warning 的期；
- 出现跨期重复素材；
- 全量测试有 failed；
- `git ls-files --deleted` 非空。

---

## 八、风险与预案

| 风险 | 预案 |
|---|---|
| 改动破坏现有 744 测试 | 每个改动跑全量回归；破坏即回退该提交 |
| B站适配器实际不可用 | T5 降级为「接线但不启用」，记录实测结论 |
| 视觉确认误杀（产品名只在画面无语字） | 全部 miss 时判 `inconclusive` 不剔除，只标注 |
| 「准优先」导致体积不足 70 MB | 允许，但必须在交付说明显式标注 |
| 7 期批次超时/被限流 | 每期间隔 120s；后台运行；失败期单独重跑 |
| 单 worker 撑不住长任务 | 批次由主 agent 用后台任务亲自启动，不委派 |

---

## 九、明确不做的事

- 不改动点名 4 期的任何产物。
- 不引入需要人工确认的环节。
- 不新增重型依赖（不装 torch/transformers；如需 CLIP 类模型另行决策）。
- 不修改 `AGENTS.md` / `docs/项目开发过程文档.md` 等文档基线（除非用户明确要求）。
- 不做 `git add -f` 强制纳管。
