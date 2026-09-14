# 单次开发指导文档：素材采集多源化（SourceAdapter 抽象 + Douyin / yt-dlp / bilibili 源）

- 日期：2026-09-14
- 状态：**已落地**——适配器基座 + 三个真实源（`douyin` / `ytdlp` / `bilibili`）；`config` 可选键与 `materials` Referer 已落地。**未完成且未获用户批准**：多源聚合/去重、命中率闸门、体积核算剔除、下载侧接线（见 §6）。
- 项目：`douyin-content-intelligence`
- 上游需求：用户 2026-09-14 实证（题材 `Microduck 机械鸭机器人`：交付 121 MB、`status=success`、`degraded=false`，但 `04-原片` 仅 3 条且**无一条为 Microduck**；日志 warning「9 个搜索词均未在任何候选标题中命中，下载顺序退化为热度 → video_id」）
- 用户需求原文：「片源不一定得来自抖音吧……只是为了获取及时有效对应的素材，不一定非得从抖音获取吧，视频号的下载渠道也是跑通的……没必要在抖音死磕」
- 依据事实：`AGENTS.md`、`docs/handoff/README.md`、`CURRENT_STATUS.md`、`CODE_MAP.md`、`DECISIONS.md`、`RUNTIME_SAFETY.md`、`PRODUCT_RULES.md`
- 相关提交：`447ba87`（sources 包 + config + materials）、`9082bd8`（交付目录名主题上限 12→24）

> 本文件是本项目契约要求的 task guide。**文档只描述已落地的真实类名/方法签名/路径**；尚未落地的部分在 §6 显式标注为「未完成/待批准」，不得当作已完成。

---

## 1. 目标

把「素材发现层」抽象为可插拔的 **`SourceAdapter`**，使：

1. **抖音保留**，但定位改为**对标平台 / 热度参考**，不再是唯一素材来源；
2. **素材获取多源化**：加一个平台 = 加一个适配器文件 + 一行配置，核心流程零改动；
3. **下载层保持平台无关**（`materials.download_video` 只吃 URL），不因多源而分叉；
4. **零命中不再假达标**：某源搜索词在该源候选标题中命中率过低时，不再「warning 后照样下载交付」，而是按源闸门处理（缺省关闭，显式开启）。

**第一优先级硬约束（铁律）**：**不配 `material_replication.sources` 键时，抖音单源路径行为与改动前逐字节等价。** 为此全部改动采用「新增 + 可选键 + 控制流分叉」，绝不在旧路径上重写逻辑。

---

## 2. 背景与实证

### 2.1 结构事实

| 事实 | 出处 |
| --- | --- |
| 下载层已平台无关：`download_video(url, destination, config, *, max_bytes=None, referer=None)` 只吃 URL | `materials.py`（`447ba87`） |
| `ytsearchN:<kw>` 伪 URL 可做**搜索发现**（不只下载） | `sources/ytdlp.py` |
| 媒体签名 URL 只在 `before_sanitize` 回调中捕获进内存、**从不落盘** | `sources/douyin.py` / `artifact_safety.py` |

### 2.2 抖音的失效边界（六期完整批次，6/6 `EXIT=0`）

| 题材 | 类型 | 原片 | 命中率 |
| --- | --- | --- | --- |
| 特斯拉 Cybercab / 苹果 iPhone Duo / DeepSeek V4.1 Flash / 大疆 Osmo Pocket 4 Pro | 全民或国内 | 8 / 8 / 6 / 5 | **100% × 4** |
| iRobot Roomba 875 | 海外 | 4 | **25%** |
| Microduck 机械鸭机器人 | 海外小众前沿 | 3 | **0%** |

⇒ **抖音对「全民关注 + 国内产品」完全够用，只在「海外小众前沿硬件」上崩掉。** 第二源是**补一小类**，不是全面替换抖音。

### 2.3 体积达标签会被废片骗过

`U技_Unitree G1 学会了更多奇奇怪怪_7552823944370277668.mp4`（81,552,961 B）**在两期各出现一次、字节数完全相同**（两期都退化成「按热度下载」）。期 1 目录 121 MB、期 3 目录 126 MB，**看似都落在 70~150 MB 区间**，但扣掉这条废片后**有效内容只有 ≈ 39.5 MB / ≈ 44.5 MB，均低于 70 MB 下限**。
⇒ 体积核算必须**剔除题材不相关的片子**，否则体积闸门形同虚设。

### 2.4 闸门判据必须是「命中率阈值」而非「零命中」

现判据「**只要有一条命中就不报警**」，导致 Roomba 期 25% 命中却 `warnings: []`（**假阴性**）。正确判据应为 `hits / total >= min_hit_ratio`。

---

## 3. 范围

### 3.1 已落地

| 需求 | 落地要点 |
| --- | --- |
| 可插拔源接口 | `sources/base.py` 的 `SourceAdapter` 协议 + `SourceResult`；`sources/__init__.py` 注册表 |
| 平台无关取流 | `MediaResolver.resolve_target(candidate) -> DownloadTarget \| None`；URL 与 Referer 同源同调，仅存内存 |
| Referer 解耦 | `download_video(..., *, referer=None)`：`referer or "https://www.douyin.com/"`，缺省逐字节兼容 |
| 抖音源 | `sources/douyin.py` 的 `DouyinSource`（薄包装既有搜索链） |
| 第二/三源 | `sources/ytdlp.py` 的 `YtDlpSource`；`sources/bilibili.py` 的 `BilibiliSource` |
| 配置可选键 | `jobs.material_replication.{sources, source_budgets, source_gate}`（均**可选**，缺省零影响） |

### 3.2 未完成 / 待批准（见 §6）

多源聚合 / 去重 / 预算分配（`source_aggregation.py`）、命中率闸门、《体积核算剔除不相关片》、`Candidate.source` 等字段、`media_urls → media_resolver` 3 处调用点、`invoke_downloader` 的 referer 透传、`search_attribution.per_source`、跨期去重持久化、`wechat_channels` 源。

### 3.3 安全边界

- 签名/直链 URL **永不落盘**：只在 `DownloadTarget.url` / `resolve_media_url()` 返回值中经内存传递；`SourceResult.report` 按构造白名单化（`YtDlpSource.SENSITIVE_REPORT_KEYS`、`DouyinSource._CRAWL_REPORT_KEYS`、`BilibiliSource._REPORT_KEYS`）。
- bilibili 媒体直链是**短时签名链**（query 含 `e` / `deadline` / `gen` / `upsig`），必须**立即消费、绝不落盘**。
- 不读取/复制/提交 Cookie、API Key、浏览器用户目录、`.env.local`。
- 抖音素材仍仅作**发现与关注度证据**；清单须含 `evidence_disclaimer`。
- 有界执行：bilibili 节流（默认 2.0s）+ 412 退避（最多 4 次，`3 * attempt` s）；yt-dlp 每源默认 `socket_timeout=120s`。

---

## 4. 设计

### 4.1 源协议（`sources/base.py`）

```python
class SourceAdapter(Protocol):
    name: str
    download_referer: str | None          # None = 平台无关 CDN

    def search(self, keywords: Sequence[str], budget: int, *,
               config: dict[str, Any], run_id: str | None = None) -> SourceResult: ...

    def resolve_media_url(self, candidate: "Candidate") -> str:
        """无可用地址 → 返回 ""；后端/网络错误 → 抛 MediaResolutionError（单条失败）。
        安全契约：返回的 URL 只在内存中传递，永不落盘。"""
```

- `search` 返回 **`SourceResult`（而非裸 `list[Candidate]`）**：把「可落盘的候选/报告」与「只在内存的取流能力」分开，保住签名 URL 不落盘的安全属性。
- `resolve_media_url` **惰性且逐条**（yt-dlp/bilibili 需二次取流；douyin 直接查内存表）。

### 4.2 取流解析器

```python
@dataclass(frozen=True, slots=True)
class DownloadTarget:
    url: str
    referer: str | None = None

class MediaResolver(Protocol):
    def resolve_target(self, candidate: "Candidate") -> "DownloadTarget | None": ...

class DictMediaResolver:   # 旧抖音路径；DOUYIN_REFERER = "https://www.douyin.com/"
class CompositeMediaResolver:  # 多源：按 candidate.source 分派（待 Candidate.source 落地后启用）
```

用 `None`（而非空串）表示「无可用地址」，避免「无目标」与「空 URL 目标」混淆。

### 4.3 注册表（`sources/__init__.py`）

```python
register_source(name, factory)   # factory() -> 全新适配器
get_source(name) -> SourceAdapter # 未知源 → SourceError
known_sources() -> frozenset[str] # 配置 sources 白名单的单一真源
```

**实际状态**：`known_sources()` 返回 **`{"douyin", "ytdlp", "bilibili"}`**（`wechat_channels` 待其 endpoint 配置后加入）。

### 4.4 多源聚合插入点（待批准）

聚合应插在「采集 → 预筛」之间，早于任何下载与预算分配：

```
collect_candidate_pool(theme)
  ├─ names = 配置 sources（缺省 ⇒ []，走旧抖音路径，逐字节不变）
  ├─ for name in names: get_source(name).search(keywords, per_source_budget) → SourceResult
  ├─ merge + 去重（dedup_key = "{source}:{原生id}"，冲突按 sources 优先级保留）
  ├─ 逐源 relevance_report → 命中率 < min_hit_ratio → 按 on_zero_match 处理   ← 闸门
  ├─ 闸门后为空 ⇒ pool["no_match"]=True
  └─ media_resolver = CompositeMediaResolver({name: adapter})
prefilter → relevance → DownloadBudget → 下载（media_resolver.resolve_target）→ 交付
```

**闸门位置为硬约束**：必须在 `collect_candidate_pool` 聚合返回后、`prefilter` 与 `DownloadBudget` **之前**短路，不能只依赖下游 `relevance.degraded` 告警（那正是 Microduck 121 MB 假达标的成因）。

### 4.5 文件清单

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `src/douyin_intelligence/sources/__init__.py` | 已落地 | 源注册表 |
| `src/douyin_intelligence/sources/base.py` | 已落地 | 协议/异常/`SourceResult`/`DownloadTarget`/`MediaResolver` 实现 |
| `src/douyin_intelligence/sources/douyin.py` | 已落地 | `DouyinSource`（包装 `collect_search`+`normalize_candidates`+`media_url_map`） |
| `src/douyin_intelligence/sources/ytdlp.py` | 已落地 | `YtDlpSource` |
| `src/douyin_intelligence/sources/bilibili.py` | 已落地 | `BilibiliSource`（纯 stdlib wbi 搜索） |
| `src/douyin_intelligence/source_aggregation.py` | **未完成** | 预算分配 / 聚合去重 / 命中率闸门 |
| `src/douyin_intelligence/replication_candidates.py` | **未完成** | `Candidate.source` 等字段；多源分支 |
| `src/douyin_intelligence/replication_pipeline.py` | **未完成** | `media_resolver` 替换 `media_urls`；`per_source`；`no_match` |
| `src/douyin_intelligence/replication_selection.py` | **未完成** | `invoke_downloader` 透传 referer；2 处调用点 |
| `src/douyin_intelligence/materials.py` | 已落地 | `download_video(..., *, referer=None)` |

---

## 5. 已落地实现（真实签名）

### 5.1 `sources/base.py`

- 异常：`SourceError`（整源失败）、`SourceNotConfigured`（已注册但平台未接通）、`MediaResolutionError`（**单条**取流失败）。
- `SourceResult`（`@dataclass(slots=True)`）：`source, status("success"|"empty"|"failed"|"no_match"), candidates, keywords_requested, keywords_used, report, warnings=[], error=""`。
- `SourceAdapter`（`@runtime_checkable` Protocol）：`name`、`download_referer`、`search(...)`、`resolve_media_url(candidate) -> str`。
- `DownloadTarget`（`@dataclass(frozen=True, slots=True)`）：`url: str`、`referer: str | None = None`。
- `MediaResolver`（Protocol）：`resolve_target(candidate) -> DownloadTarget | None`。
- `DictMediaResolver(mapping)`：`DOUYIN_REFERER = "https://www.douyin.com/"`。
- `CompositeMediaResolver(by_source)`：按 `getattr(candidate, "source", "")` 分派；未知源 → `None`。

### 5.2 `sources/douyin.py`

- `DouyinSource`：`name="douyin"`、`download_referer="https://www.douyin.com/"`；`__init__(*, collector=None)`。
- `search(...)`：薄包装 `collect_search`（`before_sanitize=capture` 捕获原始行）+ `normalize_candidates`/`compute_heat_scores`/`media_url_map`；`report` 经 `_CRAWL_REPORT_KEYS` 白名单；采集异常 → `status="failed"` 不抛穿。
- `resolve_media_url`：查内存 `self._media_urls`（签名 URL 从不进 `SourceResult`）；无则 `""`，**不抛** `MediaResolutionError`。

### 5.3 `sources/ytdlp.py`

- `YtDlpSource`：`name="ytdlp"`、`download_referer=None`；`__init__(*, timeout_seconds=None, ydl_factory=None)`。
- `DEFAULT_TIMEOUT_SECONDS=120`、`MIN_PER_KEYWORD=10`、`SENSITIVE_REPORT_KEYS`。
- `search`：每词 `ytsearch{per_keyword}:{keyword}` + `extract_flat=True`；`yt_dlp` 不可导入 → `status="failed"`；`report` 手写白名单。
- `resolve_media_url`：二次 `extract_info` + `_pick_media_url`（优先 progressive MP4）；后端错误 → `MediaResolutionError`。
- `video_id = f"yt-{raw_id}"`（`:` 是 Windows 非法文件名字符）。

### 5.4 `sources/bilibili.py`（纯 stdlib）

- `BilibiliSource`：`name="bilibili"`、`download_referer="https://www.bilibili.com/"`；`__init__(*, fetcher=None, sleeper=None, timeout_seconds=None)`。
- 纯签名函数（可单测）：`mixin_key_from(img_key, sub_key)`、`compute_w_rid(params, mixin_key)`、`sign_params(params, mixin_key, *, wts=None)`。`MIXIN` 索引表、`_SIGN_STRIP = "!'()*"`。
- 端点：`/x/web-interface/nav`（取 wbi 钥匙）、`/x/web-interface/wbi/search/type`（发现，一次 20 条含 `title`/`pubdate`/`author`/`play`/`bvid`）、`/x/web-interface/view`（取 `cid`）、`/x/player/wbi/playurl`（取流）。
- `_UrllibFetcher`：持有一个常驻 `CookieJar`（首页预热 `buvid3`/`b_nut`），`urllib` 无第三方依赖。
- 节流与退避：`DEFAULT_SLEEP_SECONDS=2.0`（`bilibili_sleep_seconds` 可覆盖）、`MAX_ATTEMPTS=4`、`RETRY_BACKOFF_BASE=3.0`；`HTTP 412` 视为**频率风控**并退避重试。
- 请求护栏：`MAX_PAGE_SIZE=20`、`MAX_KEYWORDS=8`（超出记为 `keywords_truncated`）。
- `resolve_media_url`：两跳（`view`→`cid`，`playurl`→`durl[0].url` 或 `dash.video[0].baseUrl`）；**任何失败返回 `""`**（本适配器有意不抛 `MediaResolutionError`）。
- `video_id = bvid`；`duration_source="bilibili.duration"`。

### 5.5 `config.py`（可选键校验）与 `materials.py`

- `sources`（数组，成员须在 `known_sources()`）、`source_budgets`（对象，每值 ≥1）、`source_gate`（对象；`enabled` bool；`on_zero_match ∈ {warn, skip_source, fail_source}`）。**缺省即跳过**，`load_config` 对旧配置逐字节等价。
- `download_video(url, destination, config, *, max_bytes=None, referer=None)`：`headers["Referer"] = referer or "https://www.douyin.com/"`。

### 5.6 `replication_theme.py`

- 交付目录名主题上限 `max_theme` 12 → **24**（`9082bd8`），避免主题被截断。

### 5.7 测试

`tests/test_sources_base.py`、`tests/test_douyin_source.py`、`tests/test_ytdlp_source.py`、`tests/test_bilibili_source.py`、`tests/test_materials_referer.py`。
基线：**744 passed**（裸跑 `pytest tests`）；或 **733 passed**（`--ignore=tests/test_workbench_launcher.py`，该文件 11 例）。**本项目无 deselect 机制**。

---

## 6. 未完成 / 未获用户批准

1. `Candidate.source`（及 `dedup_key`/`also_seen_in`）字段；`to_dict()` 对新增源字段的条件序列化。
2. `source_aggregation.py`：预算分配 / 聚合去重 / **命中率闸门**。
3. 闸门判据从「零命中」升级为 `hits / total >= min_hit_ratio`。
4. **体积核算剔除不相关片**（防 §2.3 的废片凑量）。
5. `media_urls → media_resolver` 的 3 处调用点（`replication_pipeline.py` 1 + `replication_selection.py` 2）。
6. `invoke_downloader` 的 `referer` 透传（沿用其 `inspect.signature` 按需透传模式，避免打断 3 参离线 fake）。
7. `search_attribution.per_source` 与清单多源归属。
8. **跨期去重持久化**（同一废片跨期重复入选）。
9. `wechat_channels` 源（调用形态未确认）。

---

## 7. 验收

- `python scripts/audit_handoff.py --root .` **7/7 OK**；`tests/test_handoff.py` **3 passed**。
- 全量 `pytest tests`：**744 passed，0 failed**。此前记录的"3 个预存在失败 + 按项目约定 deselect"不成立——那 3 个是 LF-only 批处理启动器缺陷打坏的 workbench 用例，已在 `0341c0e`/`aedcdad`/`47143aa` 修复并转绿。
- 各源 `search` 在注入 seam（`ydl_factory`/`fetcher`/`collector`）下离线正确映射；其 `report` 经白名单扫描无签名 URL。
- **向后兼容**：不配 `sources` 键时旧抖音路径逐字节等价（批次二落地时由回归守卫验证）。
- **命中率**：某源命中率低于阈值 ⇒ 按源闸门处理；多源下不因单源低命中而整体失败。

---

## 8. 风险与待明确

- [ ] `source_gate` 裁决：缺省 `enabled=false`、`on_zero_match="skip_source"`。**注意** `config.py` 的类型校验处对 `enabled`/`on_zero_match` 用的缺省字面量是 `True`/`"warn"`（**仅用于类型/成员检查，不构成运行期默认**）——读取时必须以裁定的 `false`/`skip_source` 为准。
- [ ] `download_video` 已收可选 `referer`，但**透传**（`invoke_downloader` + 3 处调用点）尚未做，在此之前非抖音源仍可能带抖音 Referer。
- [ ] **跨源热度不可比**：YouTube `view_count` 与抖音 `play_count` 量级差异大；多源模式下宜先逐源归一。
- [ ] bilibili 密集请求触发 `HTTP 412`（频率风控，非缺表头）；必须节流 + 退避，勿误判为「源不可用」。
- [ ] `publish_time_type` 为抖音专有；yt-dlp/bilibili 无对应键。
