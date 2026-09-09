# 2026-08-28 候选账号池 V1

## 目标与阶段边界

在现有中文科技内容工作台增加“候选账号池 / 账号体检”：用户可添加高粉科技资讯候选账号，以北京时间最近14个自然日、每账号最多30条公开作品元数据进行可解释评估，并由用户手工晋级、暂停或排除。候选账号绝不自动成为可信账号，也不自动接入财研网AI排行榜。

本阶段到账号池、元数据体检、中文报告、工作台人工管理和两个示例候选的有界真实验收为止。完成后停止；不开发素材搜索、跨账号新闻热榜、跨账号事件聚类、昨日榜或 OpenMontage 集成。

## 当前兼容基线

- 财研网AI单账号采集、视觉OCR、确定性热度底账与独立AI易读简报必须保持。
- 当前模型为用户明确授权的不安全远程HTTP模式，但本任务不得调用模型，也不得暴露endpoint、Key或OCR全文。
- 项目浏览器只使用既有专用持久profile与CDP配置，复用单实例；登录/验证码最多保留一个项目页面，正常结束沿用现有关闭策略。
- `CopySkillDailyTechNews`保持未安装、未启用、未运行。
- 当前目录不是Git仓库不构成失败；不得修改第三方MediaCrawler或OpenMontage。

## 账号池数据模型

项目自有原子状态至少保存：

- `account_id`：稳定、唯一；
- `display_name`、规范化Douyin用户主页URL；
- `lifecycle_status`：`candidate | trusted | rejected | paused`；
- `enabled`、`added_at`、`updated_at`、有界用户备注。

`evaluation_recommendation`与生命周期严格分离：`core_candidate | supplemental_candidate | do_not_use | insufficient_data`。算法只能给建议，不能写入`trusted`。添加、状态变更与备注更新采用路径约束、原子替换和进程内互斥；重复主页/ID、非法或非Douyin用户URL返回明确中文错误。

初始真实验收候选仅为观察对象：量子位与36氪。配置或代码不得把二者自动设为可信；真实身份不符时标记`identity_mismatch`并停止该账号推荐。

## 14日体检与确定性指标

- 时间窗：`Asia/Shanghai`最近14个自然日，包含目标日；窗口起点为第13天00:00，终点为目标日23:59:59.999999。
- 每账号最多保留30条，经账号身份和时间边界过滤后本地截断；上游多返回不追加请求。
- 指标：`total_posts`、`active_days`、`posts_per_active_day`、科技新闻相关数量/比例、短视频比例与时长缺失率、疑似广告/课程/纯推广比例、互动中位数与缺失率、identity_status、data_quality、recommendation及逐项理由。
- 缺少时长保持`null`并进入缺失率，不作为0秒；缺互动不作为0热度。
- 科技新闻相关度和推广判断只使用标题/描述的透明关键词规则，不调用LLM、不冒充事实核验。

阈值集中在非秘密配置并回显：核心候选参考活跃>=10天、科技新闻相关度>=60%、短视频比例>=60%、推广比例<=30%；补充候选参考活跃>=5天且内容垂直度达标。关键字段缺失或样本不足时降级`insufficient_data`，不得为通过而填0或编造。

## 资源与媒体预算

- 默认且本次真实验收只抓元数据：媒体下载=0、ASR=0、LLM=0、OCR=0。
- 可保留未来显式有限视觉抽样扩展点，但本阶段不实现真实媒体流程；若配置存在，硬上限必须为全局3个样本且`audio_attempted=0`。
- 每账号最多30条、账号数受当前运行选择限制、并发1、有限浏览器/网络/CDP/子进程超时。
- 登录、二维码或CAPTCHA只尝试一次并返回`needs_login`；不得循环重试或扩大到其他账号。

## 失败语义

运行状态为`success | partial | empty | needs_login | failed`：

- 单账号失败保留其他账号成功结果并使整体`partial`；
- `identity_mismatch`阻止合格建议；
- 空作品是`empty`，不得伪装采集失败；
- 登录/验证码为`needs_login`，保留一个项目页面；
- 所有结果即使失败也原子写出确定性报告，不影响财研网AI已有产物。

## 交付物

- 项目自有账号池状态文件与CRUD/生命周期服务。
- 元数据体检模块、CLI入口与工作台候选账号区域。
- `output/account-pool/<target-date>/account-evaluation.json`与`account-evaluation.md`。
- 报告包含运行预算、账号身份、指标、建议理由、数据缺失、普通主页链接、状态和脱敏错误，并声明抖音仅用于活跃度、内容形态和热度观察，不是新闻事实证据。

## 工作台合同

不重写现有布局；增加清晰区域以查看账号、添加候选、启用/暂停、体检选中/全部启用候选、打开最新报告、手工晋级正式账号或排除。状态显示等待登录、运行中、完成、部分完成、数据不足、身份不匹配。重复点击由既有任务锁/worker状态拒绝，不创建第二worker或浏览器窗口。现有财研网AI、OCR、AI简报与一键启动保持。

## 安全与排除项

- 不读取`.env.local`、密码、Cookie、二维码、profile内容；不持久化签名媒体URL、WebSocket、endpoint、Key、Authorization或profile路径内容。
- 不下载媒体、不运行ASR/OCR/LLM；不安装、启用或运行计划任务。
- 不修改`third_party/MediaCrawler`、OpenMontage、现有热度公式或财研网AI底账。
- 不提前开发素材搜索、多账号新闻排行、跨账号聚类或昨日自然日任务。

## 验收矩阵

1. CRUD、URL规范化、重复主页/ID、非法URL和原子写入。
2. 四种生命周期与手工晋级/排除/暂停；算法不自动晋级。
3. 北京14日边界、30条上限、账号身份过滤。
4. active_days、各比例、中位数、缺失率和阈值边界。
5. 时长/互动缺失不当0；样本不足诚实降级。
6. identity_mismatch阻止推荐；单账号失败使整体partial且保留其他结果。
7. 默认媒体/ASR/OCR/LLM调用均为0；有限视觉扩展上限不超过全局3且音频为0。
8. needs_login、项目浏览器单实例/页面生命周期与重复worker拒绝。
9. JSON/Markdown原子输出与敏感字段清理。
10. 工作台候选入口及现有财研网AI、OCR、AI简报、启动器回归。
11. doctor、CLI/GUI离线smoke、敏感扫描、只读scheduler状态和handoff audit。

如触及浏览器、worker、锁或子进程，首轮聚焦和首轮完整pytest均在隐藏独立Windows进程组运行。

## 真实验收与停止条件

离线验证通过后，只对量子位和36氪各做一次有界真实元数据体检：14日、每账号最多30条、并发1、媒体/ASR/OCR/LLM为0、一个项目浏览器。核对实际身份、作品数、活跃天数、指标和产物路径。登录/CAPTCHA时停止外部步骤、不重试，并把真实结果与实现/离线通过分层报告。

完成CURRENT_STATUS与README维护，以append helper追加过程记录但不读取旧文档，运行handoff audit后停止，等待项目总监下发第二阶段。
