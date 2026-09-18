# 2026-09-17 研究台账接线 v1（单次开发指导文档）

> 一句话：**把"理解"交给 agent，把"通道"写成代码。** agent 联网查资料、填成一张格式冻结的"台账"；代码只负责把台账搬进研究包，不查资料、不做判断。

## 1. 背景与问题

`episode-research-pack`（跨仓冻结契约 v2，消费端 Haike 已实现 64 KB 读取器）每期自动产出，但 `claims.json` / `sources.json` **恒为空**，`disposition` 恒为 `research_required`。原因：上游 `research_inputs` 注入口**存在但无人喂数据**——CLI `episode-research-pack build` 和流水线 `_publish_research_pack_for_delivery` 都不传它。

用户已否决"Python 抓取器自动判断真假"的路线（机器分不清"十个转载=一个来源"）。定案：**内容由 agent 产出**（agent 有理解力），**代码只做通道**（搬运、校验、渲染）。

## 2. 本次目标（验收口径见 §7）

彻底打通「agent 台账 → 研究包」链路：

1. 新增 `research_ledger.py`：台账 JSON 的读取、校验（对齐冻结枚举）、按 theme+date 自动发现、人读 Markdown 渲染。
2. 接线：流水线自动发现台账并喂给 `publish_from_delivery`；CLI 增加 `--ledger` / `ledger-check` / `render-ledger`。
3. 内容：产出真实的「2026-09-17 平陆运河」台账（≥8 sources、≥15 claims），把该期空壳 r1 升级为有内容的 r2。
4. 测试：契约级单测 + 端到端证明（r1 不可变、r2 有内容、无台账时行为逐字节不变）。

## 3. 非目标（本次不做）

- 无人值守/定时调度（用户明确排除）。
- 代码自动联网抓取、LLM 抽取（路线已否决）。
- 改动冻结契约本身（`episode_research_pack.py` 的 schema、枚举、hash 规则**一律不碰**）。
- 触碰 `scripts/build_report_doc.py` 及其测试（另一条工作流的未提交产物）。

## 4. 冻结接口（两个 worker 并行开发的契约，双方都照此实现）

新模块 `src/douyin_intelligence/research_ledger.py`：

```python
LEDGER_SCHEMA = "episode-research-ledger/v1"

class ResearchLedgerError(ValueError): ...

def load_research_ledger(path) -> dict:
    """读取并校验台账 JSON，返回 research_inputs 字典：
    {"sources": [...], "claims": [...], "topics": {...}?, "audience": {...}?, "disposition": str?}
    校验失败抛 ResearchLedgerError（中文错误信息，点名字段）。"""

def discover_research_ledger(config, *, theme: str, business_date: str) -> Path | None:
    """在配置目录中找匹配台账；找不到返回 None（不抛异常）。"""

def render_ledger_markdown(inputs: dict, *, theme: str, business_date: str) -> str:
    """渲染人读《事实台账.md》：分节、逐条带 URL+发布日+信源等级、冲突并列。"""
```

台账文件格式（v1）：

```json
{
  "schema": "episode-research-ledger/v1",
  "theme": "平陆运河",
  "business_date": "2026-09-17",
  "episode_id": "2026-09-17-平陆运河",
  "sources": [ {frozen source 字段} ],
  "claims":  [ {frozen claim 字段} ],
  "topics": {...}, "audience": {...}, "disposition": "partial"
}
```

- `sources[]` 字段与枚举**逐一对齐** `episode_research_pack.py` 的 `_validate_sources`：source_id 唯一；authority ∈ SOURCE_AUTHORITIES；verification_state ∈ VERIFICATION_STATES；heat_only 为 bool；freshness{observed_at 非空, policy ∈ FRESHNESS_POLICIES, status_at_publish ∈ FRESHNESS_STATUSES}；另带 publisher/title/url/published_at/excerpt。
- `claims[]`：claim_id 唯一；evidence_status ∈ EVIDENCE_STATUSES；wording_policy ∈ WORDING_POLICIES；freshness_requirement ∈ FRESHNESS_REQUIREMENTS；source_ids 引用必须存在；fact_sources_present == 已核验事实源数；fact_sources_min 与 evidence_status 匹配（confirmed_official=1、confirmed_two_reliable=2）；confirmed_two_reliable 须 ≥2 个独立 publisher。
- **校验复用**：loader 组装 `{"sources.json":…,"claims.json":…,"materials.json":{"materials":[]}}` 后调用 `episode_research_pack._validate_sources(payloads, errors)` 做交叉校验，不重复造轮子。

## 5. 任务拆分（3 个 worker 并行 + 1 个 QA 收尾）

| Worker | 文件（互不重叠） | 内容 |
|---|---|---|
| W1 工程师 | 新建 `src/douyin_intelligence/research_ledger.py`、`tests/test_research_ledger.py` | 模块四函数 + 契约级单测 |
| W2 工程师 | `config.py`、`replication_pipeline.py`、`cli.py`、`tests/conftest.py`、`config/content_intelligence.json` | 接线：配置键、流水线自动发现、CLI 三个动作 |
| W3 工程师 | 新建 `input/research-ledgers/2026-09-17-平陆运河.json` | 真实台账内容（agent 产内容） |
| W4 QA | 只读 | 对抗性验证 §7 全部条目 |

### W2 接线细则

- 配置：`jobs.material_replication.episode_research_pack.ledger_root`，默认 `"input/research-ledgers"`，须为项目内相对路径（无盘符、无 `..`）。`research_pack_settings()` 返回它。
- 流水线：`_publish_research_pack_for_delivery` 在 `settings["enabled"]` 后 `discover_research_ledger(config, theme=theme, business_date=business_date)`；找到则 `load_research_ledger` 并把结果作 `research_inputs` 传入 `publish_from_delivery`，追加一条 warning 注明台账路径。**只允许“确实没有台账”时退回 `research_required`；一旦发现台账但校验失败，必须显式返回研究包 `status=error` 并保留旧交付成功状态，禁止悄悄发布空包**。这样既不影响旧交付，也不会制造“看似成功、实际没内容”的假达标。
- CLI `episode-research-pack`：
  - `build --delivery-folder … [--ledger 路径]`：给了就用；没给先按 theme+date 自动发现；找到即喂入。
  - 新 `ledger-check --ledger 路径`：只校验，退出码 0/3。
  - 新 `render-ledger --ledger 路径 [--out md路径]`：写人读 Markdown（默认写到台账同目录 `<主题>-<日期>-事实台账.md`）。
- conftest：在 `_strip_opt_in_material_switches` 中对嵌套块补 `block.pop("ledger_root", None)`（登记新 opt-in 键，铁律）。

### W3 内容细则

- 素材来源：`copy_skill-main/.tmp/02-事实台账.bak.md`（361 行，带来源 URL/日期/等级），转换而非重写；可疑日期用 WebSearch 抽查。
- 信源定级：新华社/交通运输部/自治区政府 → `official`；科技日报/人民网/36氪/量子位/澎湃/财新 → `reliable_independent`；自媒体 → `heat_only`。
- `evidence_status` 纪律：工程参数类（长度/投资/船闸）有官方源 → `confirmed_official`；效益预测类双独立媒体 → `confirmed_two_reliable`；单一来源 → `creator_primary`/`unverified`；有分歧（§9 冲突项）→ `conflicting` 且 `wording_policy=hedge`。
- `freshness.policy`：工程事实 `evergreen`；新闻事件 `event_window`。
- 至少 8 sources、15 claims、2 条 conflicting 并列；`disposition` 由发布器推导（不手写）。

## 6. 开发流程

1. W1/W2/W3 并行开工（接口已冻结，无需互相等待）。
2. 各自自检：W1/W2 跑相关 pytest；W3 用 W1 落地后的 `ledger-check` 自验（时序上 W3 最后验）。
3. 主理人集成：`git status` 确认无冲突文件 → 全量 pytest。
4. W4 QA 对抗验证 §7。
5. 主理人单次提交（workers **一律不 commit**，避免 .git/index.lock 竞争）。

环境铁律（已多次踩坑）：
- Bash 前置 `export PATH="/usr/bin:/bin:$PATH"`；pytest 前置 `env -u PYTHONPATH`；解释器用 `.venv/Scripts/python.exe` 全路径。
- 本仓 pytest 不打印 summary 行：看 EXIT 码 + 点行计数。
- pytest 前后各查 `git ls-files --deleted` 必为空；禁 stash/gc；禁并发 pytest；别用 `pytest .`。

## 7. 验收标准（QA 逐条对照，全过才算完成）

| # | 判据 | 验证方法 |
|---|---|---|
| A1 | 运河台账通过校验 | `episode-research-pack ledger-check --ledger input/research-ledgers/2026-09-17-平陆运河.json` 退 0 |
| A2 | 坏台账被拒 | 改一个枚举值 → ledger-check 退 3，中文错误点名该字段 |
| A3 | r2 有内容 | `build --delivery-folder output/复刻视频/9.17平陆运河复刻视频 --ledger <台账>` → status=published，新 revision 的 `claims.json` ≥15 条、`sources.json` ≥8 条、disposition ∈ {ready, partial} |
| A4 | 证据包说人话 | 新 revision 的 `每期研究证据包.md` 列出真实来源（不得再出现"无第一手事实来源"） |
| A5 | 自动发现生效 | 同 A3 但不传 `--ledger`（靠 theme+date 发现）→ 结果同样非空 |
| A6 | 确定性 | 同输入连跑两次 build → 第二次 status ∈ {noop, reused}，不产生 r3 |
| A7 | 无台账行为不变 | 对无台账交付（如 9.17智元A3）build → 仍为 research_required、claims 空 |
| A8 | 全量测试绿 | `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest tests -x -q` EXIT=0；前后 `git ls-files --deleted` 为空 |
| A9 | 人读台账 | `render-ledger` 产出《事实台账.md》：分节、每条带来源 URL+发布日+等级、含冲突并列段 |
| A10 | 不可变性 | r1 目录字节不变（hash 对照）；`current.json` 指向 r2 |
| A11 | 内容覆盖度 | 台账不能只“数量达标”：核心维度至少覆盖 8 类（工程概况、三大枢纽、关键技术、效益、民生、建设历程/通航、收费政策、争议/不确定性）；每个维度至少 1 条可用于脚本的主张 |
| A12 | 脚本可用性 | 至少 10 条主张状态为 `confirmed_official` 或 `confirmed_two_reliable`；每条主张应是可直接改写成口播的完整句，禁止只有关键词、URL 或空泛标题 |
| A13 | 错误不可静默 | 文件不存在可退回 `research_required`；但文件存在且 schema/枚举/引用非法时，研究包必须显式 `status=error`，不得发布空包并伪装成功 |
| A14 | 信源真实性抽检 | QA 随机抽取至少 5 条事实，确认 URL 非占位符、publisher 与域名/页面主体一致、原文确实支持对应主张；任一伪造或张冠李戴即失败 |
| A15 | 下游消费验证 | 用 Haike 现有 `copy_skill_research_pack.py` 对新 r2 做真实读取，结果非 `invalid_contract`，且能读取 sources/claims/argument graph；只验证生产端自读不算完成 |

## 8. 风险与回退

- 台账内容不达标（独立源不足）→ W3 降级 evidence_status，**不得伪造第二来源**。
- 任何一步失败：删掉新增文件即回退，旧路径零改动（接线全部 opt-in、无台账时逐字节同改动前）。
