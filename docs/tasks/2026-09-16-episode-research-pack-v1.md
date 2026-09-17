# 2026-09-16 单期研究包 episode-research-pack-v1（contract v2）

Owner: producer-dev。跨项目：CopySkill（生产端）→ Haike（只读消费端）。

## Goal

在不改变旧 `material-replication` / hotspot / daily 合同语义的前提下，让单主题
`material-replication`（`production_mode = single_topic_material_replication`）在完成旧交付后，
额外发布一个**独立、不可变、可校验**的 `episode-research-pack-v1`，供 Haike 只读消费。

## Authoritative layout (frozen)

```
output/每期研究包/<YYYY-MM-DD>_研究包/<episode_id>/
    .staging/<pack_id>/        # 构建中的 stage（同 episode 根/同卷）
    packs/<pack_id>/           # 不可变已发布包
    current.json               # 唯一提交点
    latest.json                # best-effort（current 翻转后写）
    notify/<episode_id>.json   # best-effort（current 翻转后写）
```

- `pack_id = <episode_id>-r<revision>`，`revision` 从 1 起。
- consumer 永不扫 `.staging`；只读 `current.json` → `pack_path`。

## Frozen interfaces (exported from `src/douyin_intelligence/episode_research_pack.py`)

- `build_episode_pack_stage(...) -> Path`
- `validate_episode_pack_stage(stage: Path) -> dict`
- `publish_episode_pack_directory(stage, destination) -> bool`（True=复用 orphan）
- `flip_episode_current_pointer(current_path, pointer) -> None`
- `publish_episode_research_pack(config, ..., fault_hook=None) -> dict`
- 便捷层：`publish_from_delivery`、`inspect_episode_pack`、`derive_semantic_from_delivery`、
  `content_sha256` / `canonical_json` / `content_sha256_test_vector` / `safe_relative_path`。
- fault_hook 固定 phase（按顺序）：
  `after_stage_built` → `after_stage_validated` → `after_pack_directory_publish`
  → `before_current_flip` → `after_current_flip`。
- 包内文件（至少）：`episode.json` `sources.json` `claims.json` `audience.json` `topics.json`
  `materials.json` `rights.json` `revision.json` `run-report.json` `每期研究证据包.md`
  `package-manifest.json` `_READY.json`。

### Fixture builder (producer-side golden generator)

- `build_fixture_pack(output_root, *, business_date, theme, episode_id=None,
  disposition="research_required", claims=None, materials=None, revision=1) -> Path`
  —— 纯生产代码（不依赖 pytest / `tests` 模块），内部走真实生产链
  `publish_from_delivery` → `publish_episode_research_pack` → `build_episode_pack_stage`
  → `validate_episode_pack_stage`，并固定时钟；返回含 `current.json` 的 episode root。
  相同输入两次调用：`content_sha256` 与七个语义文件字节完全一致，且第二次零写入（幂等）。
  `output_root` 语义同 `episode_research_pack.output_root`（研究包根）。
- `FROZEN_CONTENT_SHA256 = "e5bc839ff60a91f29474c9bec50c8afca41fdaf367dc5e6978eae620b1cbaab8"`
  —— `content_sha256_test_vector()["content_sha256"]` 的字面冻结值，pins **算法**（不是任何真实包），
  供跨仓再实现方钉死算法。
- 金标夹具（canonical fixture，`business_date="2026-09-16"`、`theme="苹果折叠屏手机"`、默认
  claims/materials、`disposition="research_required"`，`episode_id=default_episode_id(...)`）
  的两代 `content_sha256` 冻结值（与 `output_root` 无关，消费端不读包即可 pin）。注意这与上面的
  算法向量是**两类不同的 pin**：
  - `FIXTURE_BUSINESS_DATE = "2026-09-16"`
  - `FIXTURE_THEME = "苹果折叠屏手机"`
  - `FROZEN_FIXTURE_CONTENT_SHA256_R1 = "7e20195dd253a12de0793b941e510674df9bcb4a715f205e668de39b972d2123"`
  - `FROZEN_FIXTURE_CONTENT_SHA256_R2 = "9a465cd53f0b79aad186ed878d8ea10d5f27902323243c179aacbfe04a79d215"`
  - `FROZEN_FIXTURE_CONTENT_SHA256_BY_REVISION = {1: R1, 2: R2}`
  - 生成 recipe（交给建夹具的人，只跑一条命令即可）：
    `.venv/Scripts/python.exe -c "from douyin_intelligence.episode_research_pack import build_fixture_pack; print(build_fixture_pack('data/temp/golden-build/output/每期研究包', business_date='2026-09-16', theme='苹果折叠屏手机', episode_id='2026-09-16-苹果折叠屏手机', revision=1))"`
    （r2 同法 `revision=2`；生成后整目录字节拷贝到消费端 `tests/fixtures/episode_research_pack/r1/`、`r2/`。）

## Frozen schema (contract v2)

身份键：七个语义文件 + `_READY.json` + `current.json` 用**单键** `contract="episode-research-pack-v1"`
（不再有同义 `schema_version`/`pack_kind`）；`package-manifest.json` 用 `schema="episode-research-pack-manifest-v1"`。

- `episode.json`: contract, episode_id, business_date, production_mode, theme, disposition,
  keywords[], origin_ref{origin_contract,origin_pack_id,origin_item_id,delivery_folder,manifest_path,
  manifest_sha256,asset_path,asset_sha256,asset_bytes,authority,discovery_only,items[],assets[]}, warnings[]
- `sources.json`: sources[{source_id, authority, verification_state, heat_only, title, url, publisher,
  published_at, freshness{observed_at,valid_until,policy,status_at_publish}}]
- `claims.json`: claims[{claim_id, topic_id, text, evidence_status, wording_policy, freshness_requirement,
  source_ids[], fact_sources_min, fact_sources_present, claims_to_verify[], do_not_claim[], material_refs[]}]
- `audience.json`: audience{status, summary, segments[]}
- `topics.json`: keyword_graph{seed,expanded,subject_terms,event_terms,keywords_requested,keywords_used,keywords_truncated},
  topic_candidates[], selected_topic{topic_id,selection_basis,producer_proposal}|null,
  argument_graph{topic_id, nodes[{claim_id,dim,claim,source_candidate_ids[]}], edges[{from,to,relation}]}
- `materials.json`: materials[{material_id, kind="b_roll", origin{video_id,author,title,share_url},
  permitted_use="b_roll_only", freshness_status, duration_ms,
  segments[{segment_id, start_ms, end_ms, claim_ids[], purpose, transcript_excerpt, frame_evidence_ids[]}]}]
- `rights.json`: rights[{asset_id, asset_type, origin, rights_status, license, attribution,
  redistribution_allowed, render_eligible, review_reason, material_id?}]
- `revision.json`（不入 hash）: contract, episode_id, business_date, revision, pack_id, content_sha256,
  disposition, generated_at, supersedes{...}|null, change
- `run-report.json`（不入 hash）: contract, episode_id, business_date, revision, pack_id, generated_at,
  disposition, counts{...}, ...
- `package-manifest.json`: schema, contract, episode_id, business_date, revision, pack_id, content_sha256,
  files[{path,bytes,sha256}], exclusions=["package-manifest.json","_READY.json"]
- `_READY.json`: contract, episode_id, business_date, revision, pack_id, content_sha256, manifest_sha256,
  file_count, supersedes, ready_at
- `current.json`（唯一提交点）: contract, episode_id, business_date, pack_id, revision, content_sha256,
  manifest_sha256, disposition, pack_path="packs/<pack_id>", ready_path, updated_at
- `notify/<episode_id>.json`: contract, event_id, event_type="episode_research_pack_ready", episode_id,
  pack_id, revision, content_sha256, current_path, disposition, emitted_at

枚举：
- disposition ∈ {ready, partial, research_required, rejected}
- evidence_status ∈ {confirmed_official, confirmed_two_reliable, creator_primary, unverified, conflicting, insufficient}
  （与 `daily_material_exchange.FACT_READY` 同源；`confirmed_two_reliable` 由 ≥2 个独立 publisher 的
  已核验 official/reliable_independent source 复算，不信调用方字符串）
- wording_policy ∈ {assert, attribute, hedge, prohibit}
- freshness_requirement ∈ {fresh, evergreen, manual_review}
- source.authority ∈ {official, reliable_independent, primary_creator, heat_only, unknown}
- source.verification_state ∈ {verified, unverified, failed, not_applicable}
- freshness.policy ∈ {event_window, evergreen, manual_review}; freshness.status_at_publish ∈ {fresh, aging, expired, unknown}
- rights_status ∈ {cleared, review_required, restricted, prohibited, unknown}
- rights.origin ∈ {producer_owned, licensed, platform_content, public_source, unknown}
- asset_type ∈ {video, image, audio, text_evidence, frame_capture}
- segment.purpose ∈ {visual_support, context, transition}
- 固定值：production_mode="single_topic_material_replication"、permitted_use="b_roll_only"

## Frozen rules

- `content_sha256` 只覆盖七个语义文件，固定文件名顺序，**分帧**聚合：
  `utf8(name) + NUL + ascii(len(canonical)) + NUL + canonical + NUL`；
  canonical=ensure_ascii=False/sort_keys=True/separators=(",",":")。**操作时间不入这七个文件**，
  故同一逻辑重试同 hash；业务证据时间（published/observed/valid）允许且要求保留。
  `content_sha256_test_vector()` 提供共享测试向量。
- manifest 排除自身与 `_READY.json`，双向覆盖其余文件，path 安全、bytes/SHA256 正确；READY 在 manifest 后写。
- 目录发布用 `os.replace`，目标不存在；同 pack_id orphan 身份一致则复用，不同则 `revision_collision`。
  orphan 身份 = `(content_sha256, pack_id)`（**不含 manifest 字节**）：revision.json/run-report.json
  按设计带操作时间戳，manifest 又传递覆盖它们，故同一内容两次构建的 manifest 字节不同；若比 manifest
  会把「同内容重试」误判成冲突。复用前额外要求 orphan 通过 `validate_episode_pack_stage`（拒绝复用损坏包）。
- `current.json` 唯一提交点；翻转后 latest/notify 只 best-effort。
- 相同 episode+content → noop 且 revision 不增长；内容变化精确 +1。
- before_current_flip 失败：旧 current 字节不变，不写 latest/notify。
- after_current_flip 失败：视为已提交，重试 noop 并补 advisory。

## Data constraints

- segment 整数毫秒 `0 <= start_ms < end_ms <= duration_ms`；`(material_id, segment_id)` 唯一。
- `claims.material_refs` 与 `segment.claim_ids` 双向一致。
- material refs 永不计入事实源（claim.source_ids 只能是 sources 的 source_id）。
- `confirmed_official`/`confirmed_two_reliable` 的 fact_sources_min/present 由 source 复算。
- rights：`prohibited` ⇒ redistribution/render 均 false；`platform_content` ⇒ render_eligible=false。
- 安全相对路径拒绝绝对路径 / 盘符 / UNC / `..`。
- 无第一手事实时 disposition=research_required、claims 为空，material 仅作 B-roll。

## Wiring (minimal, reversible)

- 新增 `jobs.material_replication.episode_research_pack`（`enabled` 默认 **true**，生产端想让它上线；
  显式 `false` 关闭）。测试不经由 shipped 值关闭它，而是由 `tests/conftest.py` 的
  `_OPT_IN_NESTED_MATERIAL_BLOCKS` 把该块 `enabled` 强制回 false（沿用既有 opt-in 开关约定）。
- `run_material_replication` 在旧交付 `_publish` 成功后按开关调用 `publish_from_delivery`；失败只记录
  `episode_research_pack={status:"error", error:...}` + warning，**不改旧交付状态**；开关关闭时输出逐字节不变。
- **旧交付零写入**：research publisher 默认不写旧交付任何文件（无 sidecar，不改 清单.json）；
  只在新 pack 的 origin_ref 冻结旧 manifest/item/asset hash。可选 `research_pack_ref` annotation 收敛为
  显式 helper（`annotate_delivery_manifest` / `write_delivery_manifest_ref`），由调用方显式 opt-in 且写前过旧 validator。
- **自动路径永不 annotate**：`_maybe_publish_research_pack` 硬编码 `annotate_delivery=False`；annotation
  只保留给人工 CLI（`episode-research-pack build --annotate-delivery`，或配置里的
  `annotate_delivery_manifest` 作为人工默认值）。
- CLI 新增顶层 `episode-research-pack`（build / inspect），纯离线。

## Exclusions

- 不改 Haike 仓库、`third_party/`、cookie/.env/真实 runs。
- 不发网络，不调用 LLM/ASR/TTS。
- 不把 daily 批量 discovery 作为主链；不扩张到三新闻链。
- 不提交 Git，不更新最终 handoff / 项目过程文档（主控统一处理）。

## Frozen argument_graph closed sets (resolved)

- `ALLOWED_DIMENSIONS`（13）：event_core, evidence_detail, mechanism, user_impact, action_tip,
  industry_value, use_case, constraint, method, limitation, key_number, uncertainty, visual_moment
- `ALLOWED_EDGE_RELATIONS`（5）：supports, qualifies, contrasts, causes, precedes
- 未知 dim/relation、edge 键集非 {from,to,relation}、edge 自指、三元组重复 → 报 `invalid_contract`；
  `causes`/`precedes` 子图必须无环。
- manifest 保持含 `schema` + `contract` 两键（contract-architect 最终确认，不删 contract）。

## Acceptance tests (tests/test_episode_research_pack.py, 全离线, tmp_path)

首发 r1；相同内容 noop；内容变化 r2；content hash 分帧/无时间戳/顺序无关；
manifest 双向/hash/path；before_current_flip；after_current_flip 重试；orphan 复用；revision collision；
claim/material 双向外键；evidence_status 复算；segment ms/purpose 与 rights 不变量；topics argument_graph；
跨卷拒绝/同卷断言；旧交付零写入 + 显式 annotation 兼容。
