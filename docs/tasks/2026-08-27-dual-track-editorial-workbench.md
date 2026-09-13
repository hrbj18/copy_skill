# 双轨科技选题编辑工作台

日期：2026-08-27  
状态：当前任务

## 产品目标

把现有抖音科技热点总榜升级为同时服务“科技新闻”和“科技杂谈”的编辑部选题工作台。统一原则是：**热点先保留，类型再分流，事实最后把关。** 来源不足只能限制事实措辞和发布准备度，不能删除热度、原创价值或选题价值。

## 数据模型

- 热度维度：`heat_rank`、`heat_score`、`heat_components`。只由抖音公开互动、相关视频数和时间衰减决定；官方来源、内容类型、证据状态、人工覆盖和 LLM 均不得改变名次。
- 内容类型：唯一 `primary_content_type`，可选 `secondary_content_types`；支持 `news_lead`、`creator_review`、`creator_experiment`、`creator_tutorial`、`creator_opinion`、`mixed`、`uncertain`。
- 证据状态：`verified_official`、`verified_multi_source`、`creator_primary`、`unverified_claim`、`conflicting`、`insufficient_metadata`。类型覆盖不得伪造证据升级。
- 编辑状态：`ready_for_news_script`、`research_required`、`rumor_analysis_only`、`ready_for_tech_talk`、`manual_review`、`ignored`。
- 每个主题还要包含 `claims_to_verify`、`verified_facts`、`safe_hook`、`do_not_claim`、新闻使用提示、杂谈角度和确定性 `creative_value`。无法从安全元数据判断的创作维度必须为 `unknown`。

## 工作台与人工能力

- 工作台提供抖音科技总热榜、科技新闻线索、科技杂谈灵感、待分类/待处理四个页签；卡片/详情显示热度、类型、证据、编辑状态、作者、时间、普通分享链接、互动、来源、待核主张、安全措辞和禁说内容。
- 支持标为新闻线索、评测/实验/教程/观点、混合、待分类，忽略/恢复，置顶/加入视频候选及简短备注。
- 覆盖写入项目自有的独立状态文件，使用稳定主题 ID、原子写入、锁和路径约束；不修改原始采集数据。人工类型覆盖不改变热度排序，不提升证据状态。

## 两条稳定导出

- 新闻参考：`output/editorial-board/<target-date>/news-reference.{md,json}`。未核实新闻线索仍以 `research_required` 或 `rumor_analysis_only` 导出，已核验事实与待核主张严格分开。
- 杂谈参考：`output/editorial-board/<target-date>/tech-talk-reference.{md,json}`。原创内容无需外部新闻来源，必须包含角度、讨论价值、争议点、可视化潜力、受众、三段式结构、待核主张、禁说内容和禁止照搬原创表达的说明。
- 本项目只写项目内快照，不写 OpenMontage。

## 当前样本固定语义

- M6 Mac mini：`news_lead` + `unverified_claim`，保留原热榜名次，`research_required` 或 `rumor_analysis_only`，安全措辞明确尚未获得苹果官方确认。
- 云鲸 JXUltra 对比：`creator_review` + `creator_primary` + `ready_for_tech_talk`；产品参数和性能结论进入待核验。
- Mac Studio 本地 AI：`creator_experiment` 或 `creator_opinion` + `creator_primary` + `ready_for_tech_talk`；价格、规格和性能说法不得自动成为事实。

## 安全、排除与验收

- 继续关闭不安全 HTTP LLM；无 HTTPS LLM、Voicebox 或官方源失败仍生成确定性部分结果。不得读取/输出密钥、Cookie、认证资料或签名媒体地址，不下载媒体，不修改 `third_party/MediaCrawler`，不安装/启用/运行 Windows 计划任务。
- 复用现有安全真实元数据，不扩大采集。测试覆盖热度/证据解耦、分类/编辑规则、混合双视图、事实边界、覆盖持久化/恢复/并发安全、稳定导出、元数据不足、工作台页签与动作、降级和安全边界。
- 运行聚焦测试与独立 Windows 进程组完整回归、敏感扫描、handoff audit、只读 scheduler query，并更新当前状态与仅追加过程记录。
- 只有统一热点不被来源不足删除、新闻和杂谈正确分流、事实风险清楚标记、人工覆盖真实可恢复、两类快照稳定生成，任务才算完成。
