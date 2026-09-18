# 2026-09-18 单次开发指导文档：主题优先的素材获取与 200MB 交付策略（v1）

Owner: 项目总监；implementation: DS4.1 agents；repository: CopySkill only.

## 1. 本次目标（冻结）

将单主题 `material-replication` 从“人脸规避的短切片交付”优化为“主题一致性优先、原片与优质二创共存、可被下游选择的完整素材包”。本次仅改 CopySkill，不改 Haike_video。

用户确认的产品口径：

1. `200,000,000 bytes` 是**本期最终交付文件夹**（`MM.DD<主题>复刻视频/`）的硬上限；所有文件、报告、过程数据和素材实体均计入。运行缓存、研究包、Haike 项目产物不计入。
2. “原片”指未经过自媒体二次剪辑的官方源视频。官方/原始来源是高价值加分项，但不是唯一准入条件。
3. 高热度、画面质量好、主题直接相关的自媒体解说是正常主力素材，可进入主素材或辅助素材。
4. 主持人、人脸、人物出镜都不是删除理由。`face_class` 继续作为描述性字段保留，但不得单独决定候选准入、主素材资格或交付合法性。
5. 本期不实现原声保留、采访切段、Haike 导入器或 Haike 脚本生成；这些是下游后续事项。
6. 至少支持三类题材策略：`person_or_company_event`、`official_notice_or_security_event`、`product_or_industry_trend`。策略同时决定素材排序偏好与报告/目录标签；本期不改 Haike 脚本模板。

## 2. 非目标与边界

- 不读取 Cookie、`.env.local`、浏览器配置；不修改 `third_party/MediaCrawler`。
- 不实际联网下载、不开浏览器、不做真实平台回放作为本次验收前提；所有验收必须可离线、可重复。
- 不做人物身份识别、人脸向量、人物图库或人脸截图存储。
- 不把“官方”错误标记为“已获授权”；来源性质和版权/再分发风险必须分字段表达。
- 不破坏 `episode-research-pack-v1` 的冻结接口与既有 `清单.json` 可消费语义；新增的下游选择信息使用独立目录文件，不向冻结研究包强塞字段。
- 不修改 Haike_video，也不把 Haike 的原声/脚本规则引入 CopySkill。

## 3. 设计决定

### 3.1 两阶段选材

1. **宽进候选阶段**：仍以主题主体词、时效、技术可用性、重复控制为硬门槛；人脸、主持人、口播、视觉确认不足只能影响标签/排序，不可单独拒绝。
2. **预算编排阶段**：从通过主题门槛的候选中，在最终包预算内按综合价值选择，并保持作者/来源/角色多样性。优先级为：主题直接性和事件直接性 > 明确原始/官方来源加分 > 画面/热度 > 时长；自媒体不因来源类别被硬降级。

### 3.2 题材策略档案

在 `content_intelligence.json` 新增独立的 `theme_material_profiles`。每个主题可显式指定 `profile`；未配置时使用可预测的 `default`。配置不得复用或隐式改变 `theme_keywords`、`theme_subject_terms`、`theme_event_terms` 的既有语义。

每个 profile 至少提供：允许/优先的 `source_kind`、主素材角色优先序、标题事件词权重、原始来源加分、热度权重以及报告标签。题材选择只能由显式配置或确定性默认规则决定；不得用 LLM 或模糊词表猜题材。

### 3.3 来源和素材角色标签

新增确定性、可审计标签，不以模型身份识别为前提：

- `source_kind`: `official_original`、`news_broadcast`、`creator_commentary`、`platform_video`、`unknown`；
- `source_authority`: `official`、`news_media`、`creator`、`unknown`；
- `visual_role`: `event_direct`、`subject_person`、`product_or_scene`、`news_anchor_or_reporter`、`commentary`、`unknown`；
- `recommended_usage`: `main`、`supporting`、`optional`；
- `rights_status`: `unknown`、`review_required` 等现有/明确允许的风险状态。

标签推断以候选所属平台、标题、作者和本主题的显式配置为输入；无法判断必须回落 `unknown`，不可虚构官方属性。`face_class` 必须继续透传到目录，但仅供下游描述。

### 3.4 独立的机器可读素材目录

在每个交付目录新增 `00-素材目录.json`，它是下游筛选契约，不替代现有 `清单.json`。

- 每个实体素材只放入一个物理目录（`02-主素材` 或 `03-辅助素材`）；主/辅/可选的逻辑用途在目录文件中表达。
- 目录中每条必须有稳定 `material_id`、相对 `file_path`、候选来源、标题、作者、来源/角色标签、评分、时长、文件字节、`recommended_usage`、`face_class` 与 `rights_status`。
- 路径必须是交付根下的安全相对路径；目录校验必须验证文件存在、字节一致、无重复实体路径、所有实际主/辅媒体均被列出。
- `source_url` 只能来自可公开的候选分享页；签名下载 URL 不得写入。

### 3.5 200MB 最终交付硬限制

- 以 `MAX_DELIVERY_FOLDER_BYTES = 200_000_000` 作为发布前不可突破的硬上限。
- 选择层为目录元数据、脚本、报告留出确定性安全余量；媒体入选预算使用一个比目录上限低的常量/配置值，避免发布阶段才因说明文件溢出。
- 同一文件不得跨 `02`/`03` 复制；发现重复实体或目录总量超标时交付校验失败。
- 保持下载流量预算与最终交付预算分离。`download_budget.max_bytes` 不代表交付成功。

## 4. 实施步骤与文件责任

### Step A — 契约、配置和主题模型

1. 更新 `config/content_intelligence.json`：引入三个 profile 的配置和显式主题→profile 映射；将最终目录上限配置/常量切为 200,000,000；保留旧主题三表语义。
2. 更新 `replication_theme.py`：新增纯函数读取/验证/解析 profile，提供缺省回退；不得改变现有 `expand_keywords`、`subject_terms`、`event_terms` 的返回值。
3. 更新 `replication_candidates.py` 或选择侧纯辅助函数：以可测试的确定性规则生成素材来源/角色标签，未知值安全降级。
4. 为上述纯函数补单测，覆盖显式 profile、缺省 profile、未知来源和主题词表不被 profile 影响。

### Step B — 选择、排序和人脸规则

1. 在 `replication_selection.py` 中删除把 `FACE_HEAVY` 作为通用拒绝条件、把 `FACE_FREE` 作为主素材必要条件的路径；保留人脸分析与 `face_class` 输出。
2. 将 `direct_delivery` 的特殊“人物事件豁免”演化为所有 profile 可使用的完整素材交付策略；不得改变脚本复刻选择的人脸豁免。
3. 在现有相关性、时效、技术验证之后，对候选计算可解释的选择优先级。排序至少稳定包含主题命中、事件直接性、profile 权重、来源/角色加分、平台内热度、时长和 `video_id` 兜底。
4. 保留作者上限；补一个角色/来源多样性软约束，不能因“官方”使优质自媒体完全无法入选。

### Step C — 交付、目录和容量校验

1. 在 `replication_delivery.py` 生成并校验 `00-素材目录.json`；改动原子发布路径，使该文件随交付一起生成。
2. 移除“`face_heavy` 交付错误”“主素材必须 `face_free`”的全局校验，替换为：素材目录完整、主题相关证据、标签合法、物理文件唯一、目录总字节≤200,000,000。
3. 确保报告/现有 `清单.json` 继续生成；如报告展示容量，必须显示最终目录字节和 200MB 判定，不可混用下载字节。
4. 若 episode research pack 从旧清单读主/辅文件，新增目录文件不得改变它的输入或哈希契约。

### Step D — 文档、测试和回归

1. 更新 `docs/handoff/PRODUCT_RULES.md`：删除旧的全局无脸交付规则，写入本指导文档已验证的稳定产品规则。
2. 更新 `docs/handoff/CURRENT_STATUS.md`：替换已解决的相关残余缺陷，不写聊天记录；遵守字符预算。
3. 将 `docs/handoff/README.md` 的 current task 指向本文件。
4. 仅在验证成功后，通过 `scripts/append_process_record.py` 追加一段本次过程记录。

## 5. 测试策略

所有测试必须离线、临时目录执行、无浏览器/网络/LLM 依赖。

1. **纯函数测试**：profile 解析和标签归类的正常/未知/回退分支；旧三张主题词表不受影响。
2. **选择测试**：构造同主题的官方原片、新闻主持人、主题相关高热度自媒体、无关自拍视频和 `face_heavy` 候选。
   - 前四类中的前三类均可入选；无关自拍视频被 relevance gate 拒绝；
   - `face_heavy` 的主题相关候选可以成为主/辅素材；
   - 主题相关高热度自媒体在没有更优原片时可以成为主素材。
3. **题材策略测试**：人物/企业事件、官方通报/安全事件、产品/行业趋势三种 profile 有不同且确定性的主素材排序，但都不把自媒体硬排除。
4. **交付测试**：目录生成 `00-素材目录.json`，其路径/字节/素材覆盖正确；同一实体不跨目录复制；含脸素材的交付可通过；篡改目录、重复路径、漏列文件或目录超过 200,000,000 bytes 必须失败。
5. **回归测试**：既有 `清单.json`、汇报文档和 episode research pack 相关测试继续通过；所有 opt-in 配置项必须登记 `tests/conftest.py` 的剥离列表，以保证未启用功能的旧测试夹具保持稳定。
6. **命令验证**（串行，使用 `.venv\Scripts\python.exe`）：
   - `-m pytest tests/<新增或变更的聚焦测试> -p no:cacheprovider`
   - `-m pytest tests -p no:cacheprovider`
   - `-m compileall -q src tests`
   - `scripts/audit_handoff.py --root .`
   - pytest 前后均运行 `git ls-files --deleted`，必须为空。

## 6. 验收标准（全部必须满足）

本次目标达成必须同时满足下列条件：

1. **人脸不再是硬否决**：测试证明相关的 `face_heavy` 官方原片、新闻播报或高热度自媒体可以交付；所有人脸信息仅以描述字段出现在清单/目录中。
2. **主题一致性仍是硬门槛**：不命中显式主体词的无关自拍视频在下载/交付前被拒绝；不得为了提高召回率放宽 `theme_subject_terms`。
3. **原片与自媒体并存**：目录能区分 `official_original` 与 `creator_commentary`；官方原片可获得加分，但高热度相关自媒体仍可按 profile 成为主素材。
4. **三类策略可验证**：三个 profile 都能通过离线测试产生各自可解释的稳定排序和目录标签；没有隐式 LLM 推断或跨题材词表污染。
5. **目录契约完整且安全**：每期交付有 `00-素材目录.json`；每条媒体的路径、字节、来源和选择理由可验证；没有签名下载 URL、绝对路径、`..` 路径或重复实体文件。
6. **200MB 硬限制真实生效**：最终交付目录总量超过 `200,000,000 bytes` 时验证失败；正常产物的最终目录大小在上限内；下载流量不能替代该结论。
7. **兼容性与质量**：所有聚焦测试、全量 `tests`、`compileall`、handoff audit 通过；`git ls-files --deleted` 在测试前后为空；没有修改 Haike、`third_party/`、认证材料或生成真实网络素材。

## 7. 风险与处置

- 现有测试可能把 `face_free` 当作历史契约：同步更新为“主题/标签/目录完整性”断言，避免只把常量阈值改掉的假通过。
- 目录文件增加的字节会影响容量：媒体预算必须留余量，且容量测试要包括目录/说明文件。
- 新增配置为 opt-in 时，必须加入 `tests/conftest.py` 的剥离列表；否则生产路径与测试路径脱节。
- 若真实平台缺少可判定的官方账号信息，只能标 `unknown` 或 `platform_video`，不得夸大归因。
- 任何与本指南冲突的既有 prose 在实现前以当前代码/测试为准，并在最终说明中记录具体偏差。
