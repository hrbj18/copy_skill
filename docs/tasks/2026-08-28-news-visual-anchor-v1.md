# 2026-08-28 新闻视觉锚点 V1

## 目标与阶段边界

把现有“单条已确认新闻素材探针”升级为可复用的“新闻视觉锚点”：输入最多3条已有明确事实来源的科技新闻，逐条输出最多2项具体、可追溯视觉素材——1项主视觉和可选1项备用视觉。主视觉必须让人能识别对应事件、产品、游戏、机器人型号、真实软件界面或官方公告；禁止用泛化AI大脑、蓝色电路、任意机器人、无关人物或项目生成标题卡冒充具体主视觉成功。

机器人/赛事优先现场或对应型号；游戏发布优先官方封面、商店视觉或实机/预告画面；硬件优先官方产品图或发布/演示现场；软件/模型优先真实界面、Logo或官方公告视觉。没有合格素材时诚实返回`partial/failed/needs_login`，不得用无关图补齐。

本阶段只完成受控小批量V1、兼容CLI/工作台、真实样本和视觉QA；不建设全量日榜素材库，不修改OpenMontage，不开发通用搜索框架、卫星图像、视频音频/ASR或AI泛化配图。

## 事实、模型与来源边界

- 输入故事至少包含稳定ID、标题、摘要、日期、确认状态和可追溯HTTPS来源；抖音仅提供视觉线索和关注度证据，绝不证明新闻事实。
- 大模型可选且非成功依赖，只能产生/校验视觉意图或在已有候选内二次排序；不得联网核验、补输入外事实、修改热度或绕过权利门禁。本V1优先确定性逻辑，真实验收不要求模型。
- 选择顺序：输入官方/第一方原文 → 精选中文科技RSS匹配 → RSS图片 → 已匹配文章有限图片 → 已发现官方产品/商店/媒体/预告页 → 不足时受限抖音封面/单视频帧 → 项目生成说明卡仅作备用说明，不能算具体主视觉。
- 不做漫无目的全网搜索。Openverse不是国内新闻入口；不引入SearXNG、MindSearch、DeerFlow。可参考ai-hot的MIT行为思想；若实质复制必须保留许可。worldmonitor仅按公开行为规格clean-room实现RSS图片优先级、来源计数和缓存健康，严禁复制AGPL源码。卫星图像明确排除。

## 视觉意图合同

每条新闻形成独立`visual-intent.json`：

- `visual_subject`、`subject_aliases`；
- `preferred_scene`、`fallback_scene`；
- `query_terms`最多3个；
- `negative_terms`；
- `story_region`、`category`；
- `generation_mode=deterministic|llm_assisted`与事实边界。

确定性解析须保留标题中的专名、产品/赛事/游戏实体和输入摘要，不生成新闻输入之外的实体、日期、数字或结论。恶意/越界模型结果必须拒绝并回退。

## RSS与网页图片抽取

RSS候选严格按以下优先级、每条记录取首个有效HTTP(S)图片并保留文章关联：

1. `media:content`；
2. `media:thumbnail`；
3. 图片类型`enclosure`；
4. `description`/`content:encoded`中的首个`img`。

网页最多提取`og:image`、`twitter:image`、JSON-LD `image`和3张正文相关图片。所有相对URL用文章URL解析；候选总计最多12项。只允许HTTP(S)输入，但所有实际访问遵守当前HTTPS/允许域策略；拒绝环回、私网、直接IP、假DNS与危险逐跳重定向。持久化URL移除token、signature、auth、expires等敏感查询参数。

## 抖音降级合同

仅当来源/RSS/正文没有达到相关度门槛时触发：

- 每新闻最多3个查询、检查最多8条搜索元数据/封面；封面仍不足才下载最多1个高度相关视频；
- 先用安全标题/描述做完整实体短语排序，再最多下载2张封面；封面逐跳网络请求最多4次、单张最多8MiB、累计最多12MiB，达到任一上限立即停止，并把请求数、实际字节和耗尽状态写入manifest；
- 视频最大60MB/90秒，最多抽8帧，`audio_attempted=0`，不运行ASR/Voicebox；
- 抖音封面/帧默认`reference_only`，没有明确许可绝不进入自动可渲染清单；
- 复用项目专用持久浏览器、最多一个浏览器会话/页面、并发1和JobLock；不读取密码、Cookie、二维码或profile内容；
- `needs_login`只尝试一次、保留一个页面并停止，不开重复窗口、不索要凭据；
- success/partial/failed/timeout均清理本次临时视频、帧、worker与锁，正常结束按现有策略关闭项目浏览器。

若现有受限抖音适配无法在本轮安全复用，核心来源图片路径仍须完成，并把抖音真实分支标为外部未验证；不得伪装分支通过。

## 确定性候选打分与去重

最多12个候选进入打分，最终最多2个：

- 标题、alt、caption、URL与页面上下文对exact entity/product/event的匹配为主；
- 官方/第一方/原文来源加分；分辨率、解码成功、比例合理加分；
- generic negative terms、占位图、头像、站点Logo、二维码、大营销水印、低清和无关图扣分或拒绝；
- SHA-256完全去重、感知哈希近似去重；同分按规范URL、内容哈希与稳定候选ID固定排序；
- 产品版本必须命中完整实体短语，宽泛词或版本前缀（例如目标GPT-5时的GPT-6/GPT-5.6）不能成为exact subject；同一来源、同一主体的裁切/加标题近重复只保留高分项，备用图允许缺失；
- `relevance_score`与`production_readiness`分离。模型若启用只能在已通过基础安全门的候选内二次排序，不能创造候选或越权。

## 权利与交付门禁

下载后验证MIME、真实解码、尺寸、大小、SHA-256和感知哈希；不去水印、不裁署名、不伪造许可。

- `project_generated`、`renderable_with_attribution`才可进入`renderable/`；
- 官方页、媒体文章、商店或预告画面没有明确许可时为`review_required`；
- 抖音封面/帧为`reference_only`；
- 主候选可以高度相关但不可生产使用，必须同时显示`rights_status`与`production_readiness`，不能偷放到可渲染清单。

项目生成标题/来源卡可作为备用说明，但不得作为“具体主视觉”满足成功门槛。

## 输出与兼容

输出为`output/visual-anchor/<target-date>/<story-id>/`：

- `story.json`、`visual-intent.json`、`manifest.json`、`preview.md`；
- `renderable/`、`review-required/`、`reference-only/`；
- 可选无秘密`run.log`。

manifest/preview至少记录主/备用候选、本地路径、来源类型、文章/官方页、图片来源、选择理由、得分分解、拒绝原因、权利状态、生产准备度、MIME、尺寸、字节、SHA-256/感知哈希、查询数、网络请求、抖音结果/视频/帧和`audio_attempted`，以及`success/partial/failed/needs_login`与结构化原因。

旧`material-probe` CLI和工作台入口保持兼容，可通过适配器复用视觉锚点底层；不得破坏旧输出。工作台新增或升级为“新闻视觉锚点测试”，能运行受控任务、打开最新preview/manifest或目录、显示路径/失败原因并拒绝重复启动。中文一键启动保持。

## 硬预算

每新闻：查询词<=3、候选<=12、最终持久素材<=2、单请求<=15秒、重试<=1、外部墙钟<=5分钟。抖音搜索结果<=8、视频<=1且<=60MB/90秒、帧<=8、音频0。V1批次<=3条、顺序执行。所有网络、浏览器、模型、子进程和轮询有限超时。

项目统一运行计数需明确记录；不得放宽为无界。若真实样本只走官方/RSS路径，抖音计数为0；若因登录阻塞，记录一次`needs_login`事实。

## 安全与仓库边界

- 不修改`third_party/MediaCrawler`；仅在`src/config/tests`及项目自有适配层工作。
- 不写`D:\codex_work\OpenMontage`；本轮写入必须为0。
- 不读取`.env.local`、Cookie文件、密码、二维码或浏览器profile；不输出Key、Authorization、WebSocket、敏感请求头、临时签名URL。
- URL逐跳检查，拒绝私网/环回/重绑定；错误与URL统一脱敏。
- 不事实核验、不修改新闻热度、不引入通用搜索基础设施、素材库、音频/ASR或AI生成泛图。

## 自动化验收矩阵

1. visual intent schema、确定性回退、恶意/越界模型拒绝。
2. RSS四级优先级、namespace、相对URL、无效scheme。
3. og/twitter/JSON-LD/body抽取、上限和文章关联。
4. 私网/环回/假DNS/危险重定向、URL脱敏。
5. MIME、真实解码、尺寸、大小、SHA-256/感知去重。
6. exact事件/产品/游戏稳定胜过generic AI图，无关图拒绝、同分稳定。
7. 权利目录路由；官方未知许可不自动renderable，抖音默认reference_only。
8. 最多2项；单主图可success/partial；无关图不补齐。
9. 浏览器success/needs_login/timeout/fail生命周期、单页面、视频<=1、帧<=8、音频0。
10. 所有终态清理临时视频/帧/worker/浏览器/锁。
11. 输出原子、路径安全、报告无秘密。
12. 候选账号池、排行榜、OCR/AI、material-probe、工作台与启动器不回归。

如触及浏览器、subprocess、worker、JobLock或Windows进程，首轮focused和full pytest必须在隐藏独立Windows进程组运行。

## 真实样本验收

自动化全绿后，顺序运行最多3条有可追溯官方来源的历史或当前样本，尽量覆盖机器人/赛事、游戏发布、硬件/软件/AI公告。不得为凑类型发明新闻。至少2/3主候选来自官方/原文/RSS/文章路径；抖音分支若有登录态则至少真实覆盖1条，否则只尝试一次并记录`needs_login`。

每个最终主/备用图片必须用本地图片查看能力逐张打开，记录图中内容、与新闻对应关系、水印/文字、主体错误和清晰度。制作旧material-probe与新V1基线对比：generic/generated card/人物肖像比例、exact-subject主图命中率、无关图数、各权利状态数量、请求数、耗时与抖音触发情况。

## 完成与停止条件

持续修复直到compile、focused/full tests、CLI/help/doctor、真实GUI smoke、真实网络素材、逐图视觉QA、敏感扫描、third_party/OpenMontage零写入和handoff audit完成。真人登录或外部权利是唯一阻塞时，完成其他项后精确报告最后状态、输出、窗口/进程/锁清理。

完成本V1交接后停止，不自动进入素材库、全量榜单或OpenMontage集成。
