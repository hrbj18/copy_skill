# 单次开发指导文档：同源账号矩阵选题雷达与热度去重

日期：2026-08-31  
状态：当前任务合同  
工作目录：`D:\work\copy_skill`

## 1. 背景与产品判断

用户新增三个持续追踪 AI、算力、芯片热点的抖音账号。它们在运营主体和品牌上同根同源，但内容赛道不同，均有选题发现价值。旧规则把“候选账号不得进入 V2 生产采集”与“已批准账号才计入跨账号覆盖”绑定得过紧，会丢失这类高频垂直选题雷达。

本次不把这些账号提升为事实来源、可信新闻账号或独立权威媒体，而是新增明确的 `topic_radar` 生产发现角色。同时建立运营矩阵分组：同一矩阵下的不同账号可以分别贡献不同新闻事件，但同一事件被矩阵多账号分发时不得被误算为多个独立信源，也不得用简单互动求和虚高排名。

抖音材料仍只证明内容出现及公开热度，不能证明新闻事实。

## 2. 目标

1. 将三个账号以启用的 `topic_radar` 候选身份接入每日 V2 账号发现通道，不升级为 benchmark、trusted 或事实证据。
2. 为账号配置增加稳定的运营矩阵分组和内容赛道标签。
3. 同一事件下保留矩阵所有贡献视频、账号、链接、发布时间和原始互动，但把独立来源覆盖与有效互动按矩阵去重/封顶。
4. 在 Markdown、候选 JSON、原始视频和运行报告中清楚展示原始账号覆盖、独立运营主体覆盖、内容赛道和去重依据。
5. 保持事实状态、图片状态和模型输出不参与入池或热度排序；保持 Top 3 图片、一小时预算、不可变包和同日非退化发布合同。

## 3. 新增账号与冻结身份

三个账号统一使用：

- `source_group_id`: `kaishi-dajisuan-ai-matrix`
- `source_group_name`: `大计算 / 开市科技矩阵`
- `production_role`: `topic_radar`
- lifecycle 保持 `candidate`
- `discovery_enabled`: `true`
- 不得自动成为 `approved`、`benchmark`、`trusted_creator` 或 `verified_official`

### 3.1 大计算AI产业

- 分享链接：`https://v.douyin.com/EIwlZ3HnsO0/`
- 稳定账号 ID：`MS4wLjABAAAArcRAVxpIteqgtpJ7yeKGeD2bUwheN4_Ulb2A4WV6-aqsulGktxw_ibElK2oeFjhk`
- 抖音号：`42896519316`
- `editorial_lane`: `ai_general`
- 中文赛道：AI 综合、模型发布、国际 AI 公司、开源项目

### 3.2 大计算AI产业平台

- 分享链接：`https://v.douyin.com/bxlXgzm77E4/`
- 稳定账号 ID：`MS4wLjABAAAAgq4RryZ2DMxbpwxcM012yGzkGPJUdlKSUySPWfbG3_1a3OkjIUYrE_Py-TAs3sMC`
- 抖音号：`44882093155`
- 企业认证观察：开市科技信息（浙江）有限公司
- `editorial_lane`: `compute_infrastructure`
- 中文赛道：服务器、GPU、数据中心、光模块、算力产业

### 3.3 大计算算力炼丹炉

- 分享链接：`https://v.douyin.com/QYDalJ3ymxs/`
- 稳定账号 ID：`MS4wLjABAAAAyGZquNZOuBYdFAUgPU47rW2VECNk0umlDyjeojSPPn0nZQvnda1nEoE_SdQo1VQc`
- 抖音号：`49506654128`
- 企业认证观察：开市科技信息（浙江）有限公司
- `editorial_lane`: `chips_hardware`
- 中文赛道：芯片、存储、半导体、国产硬科技

网页观察到的粉丝、活跃度和认证只属于审查快照，不写成永久信任条件。账号改名、粉丝变化或临时停更不能改变其事实权限；采集运行必须如实记录 empty/failed/timeout。

## 4. 核心产品规则

### 4.1 生产角色与信任分离

- `topic_radar` 是生产发现角色，不是信任级别。
- V2 账号通道允许采集 `enabled=true` 且满足以下任一条件的账号：既有 approved benchmark/trusted，或显式 `production_role=topic_radar` 且 `discovery_enabled=true`。
- 普通 candidate 若没有显式 topic-radar 开关，仍不得进入生产采集。
- topic-radar 候选贡献的视频与事件必须继续标注“copy_skill 未核验真实性”。
- 粉丝量、认证、互动量、更新频率和大模型判断都不得自动升级生命周期或事实权限。

### 4.2 内容赛道与运营主体分离

- `editorial_lane` 用于解释内容方向、报告筛选和 OP 选题，不用于把同一矩阵伪装成独立来源。
- 不同账号发布不同事件时，每个事件正常保留、正常排名。
- 同一事件被矩阵多个账号发布时，聚类后保留全部视频，不因同源而删除。

### 4.3 矩阵去重和排名

每个事件同时输出：

- `account_count_raw`: 原始不同账号数；
- `source_group_count`: 独立运营主体数；未配置分组的账号各自视为独立主体；
- `aggregate_interactions_raw`: 所有贡献视频原始互动聚合；
- `effective_interactions`: 排名使用的有效互动。

有效互动的冻结规则：

1. 先沿用现有单视频确定性互动公式得到每条视频贡献值。
2. 在同一事件、同一 `source_group_id` 内，仅取贡献值最高的一条视频进入 `effective_interactions`；其他矩阵视频保留在 provenance 和原始聚合中。
3. 不同独立 source group 的最高贡献值可以相加；无 group 的账号按自己的稳定账号 ID 形成独立组。
4. 跨账号/来源覆盖奖励必须使用 `source_group_count`，不得使用 `account_count_raw`。
5. `related_videos` 奖励和同分次序必须使用 `effective_video_count`（每个 source group 最多一条），原始 `video_count` 仅用于展示和 provenance。
6. 同分规则继续稳定确定；事实、图片、LLM 和人工偏好不改变排序。

为兼容旧消费者，若保留旧 `account_count` 或 `aggregate_interactions` 字段，必须明确其语义并添加新字段，不得让旧字段继续偷偷参与独立来源加权。Schema/contract 版本变化必须可审计。

## 5. 实施范围

开发工作台应在读取当前源码后完成最小、可维护实现，预计涉及但不限于：

- 版本化账号配置及配置校验；
- 账号规范化模型与账号通道选择逻辑；
- 原始视频 provenance；
- V2 事件聚类后的矩阵统计与热度评分；
- Markdown/JSON/run-report 输出；
- 与账号池、V2 候选池及交换包有关的聚焦测试；
- 稳定产品规则、当前 handoff 和必要代码地图；
- 仅以追加脚本写入本次过程记录。

开发工作台应根据当前代码定位精确文件，不得先凭历史任务文档猜测实现。

## 6. 明确排除

- 不修改 `third_party/MediaCrawler`。
- 不写入 `D:\codex_work\OpenMontage`。
- 不安装计划任务，不部署、不发布外部服务。
- 不读取或输出 `.env.local`、Cookie、浏览器 profile、API key、二维码或密码。
- 不下载音频、不做 ASR、不调用模型核真。
- 不把企业认证当成新闻事实证明或图片权利许可。
- 不改写既有不可变历史包；测试输出使用隔离目录。
- 不因为本任务扩大图片数量、关键词数量、网络预算或一小时全局预算。

## 7. 自动化测试

至少覆盖：

1. 三个冻结账号均能通过配置校验，稳定 ID、source group、赛道和 topic-radar 角色正确。
2. 显式启用的 topic-radar candidate 会进入 V2 账号采集尝试；普通 candidate 仍不会进入。
3. 同一事件由矩阵三个账号发布：三条视频全部保留，`account_count_raw=3`、`source_group_count=1`，有效互动只取组内最高贡献。
4. 同一矩阵三个账号发布三个不同事件：三个事件均保留，不因同源被跨事件删除。
5. 一个矩阵账号与另一个独立账号共同报道：`source_group_count=2`，有效互动按两个组分别取最高后相加。
6. 无 source group 的旧账号保持向后兼容，每个稳定账号视为独立主体。
7. source-group 去重影响覆盖与有效互动，但不改变原始互动、视频链接、账号、赛道和命中关键词的完整 provenance。
8. 真伪、图片、LLM 和人工字段变化不改变排名。
9. Markdown/JSON 清楚展示原始账号数、独立主体数、矩阵提示和内容赛道。
10. 单账号失败、空结果或超时能继续发布 partial，不重复打开浏览器，不影响其他账号。
11. 旧 V2 Top 3 图片、不可变包、READY/hash、同日非退化指针和 OP 消费合同不回归。

## 8. 受控真实验收

自动化通过后，对三个新增账号执行一次有界账号采集 smoke，优先使用现有项目浏览器会话和既有采集入口：

- 只验证身份解析、账号被尝试、公开元数据可进入规范化层及 source group/editorial lane 传播；
- 每账号与总流程均设置有限超时，总墙钟建议不超过 10 分钟；
- 不启用音频、ASR、OCR、LLM、图片获取或完整关键词搜索；
- 全程复用一个项目浏览器，结束后按既有规则关闭；不得留下多个窗口或孤儿进程；
- 若需要重新人工登录，停止真实验收并报告 `needs_login`，自动化成果仍可标记为实现完成但真实验收 partial；
- 若外部平台超时，记录每账号 attempted/failed/timeout、耗时与安全错误，不无限重试，不把静态测试冒充真实成功。

## 9. 验收标准

只有以下要求全部有证据时才可报告 complete；外部平台证明缺失时必须报告 partial：

1. 三个账号已作为启用的 topic-radar candidate 配置，并且没有被升级为 trusted/approved/official。
2. 每个账号的稳定 ID、运营矩阵和内容赛道配置准确且有测试。
3. V2 生产账号选择会尝试显式 topic-radar，普通 candidate 仍被排除。
4. 同事件矩阵多账号的原始内容全部保留，但独立覆盖与有效互动按 source group 去重，排名不会虚高。
5. 不同赛道的不同事件不会因同源被合并或删除。
6. 候选 JSON、Markdown 和运行报告能让 OP 同时看到账号、赛道、矩阵、原始互动和有效互动。
7. 聚焦测试、完整 pytest、compile、handoff audit、敏感信息扫描和边界检查通过。
8. 真实 smoke 有界完成并如实分级；网络失败、登录阻塞或超时不伪装为 live pass。
9. `third_party/MediaCrawler`、OpenMontage、历史不可变包、凭据、计划任务和全局预算边界均未被破坏。
10. `CURRENT_STATUS.md` 更新为本次真实结果，过程记录仅追加且未读取旧过程文档。

## 10. 停止条件

只有在必须人工重新登录、当前代码/配置与本合同存在无法安全调和的权威冲突、需要修改 OpenMontage/third_party/凭据/计划任务/付费服务，或真实采集被外部平台持续阻断时停止并报告。普通实现问题、测试失败和可安全修复的兼容性缺陷均应在本次任务内解决并复测。
