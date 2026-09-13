# 项目专用抖音浏览器会话生命周期

日期：2026-08-27  
状态：当前任务

## 用户问题与安全决定

用户报告：登录阻塞时反复运行会打开并累积多个 Google Chrome 抖音窗口，希望减少为一个，并在正常任务完成后自动关闭。用户询问能否直接提供账号密码。项目明确禁止索取、接收、读取、记录或保存账号密码、Cookie、二维码内容、浏览器认证材料；用户只能在端口 9223、独立持久 profile 的项目专用 Chrome 中人工登录。关闭浏览器进程不删除 profile 或站点存储，登录通常可复用，但抖音仍可能因会话过期或风控要求再次验证，不能承诺永久免登录。

当前诊断基线：项目专用 Chrome 只有一个根实例；Chrome 多进程不等于多个窗口。端口 9223 当前有 6 个累计 `page` target 和 2 个 `browser_ui` target。用户未授权本任务关闭当前真实项目窗口；开发、测试和离线验收关闭真实窗口数必须为 0。

## 冻结目标与设计

- 新增项目自有 `BrowserSession`/lease 边界，所有 CDP 枚举、页面收敛、关闭和浏览器退出只允许操作配置的 9223 端口与专用 profile；不得访问其他端口或个人 Chrome。
- 登录准备操作只启动或复用项目 Chrome，并确保最多一个可见抖音页面，不启动采集。采集按钮复用登录态。
- `ensure_browser()` 的启动临界区使用项目原子锁；并发调用最多启动一个实例。Windows PID检查沿用安全查询，禁止 `os.kill(pid, 0)`。
- 每次任务记录开始前 target ID；运行结束只关闭本次新建且仍存在的多余 `page` target。target/URL/WebSocket/Cookie/认证内容不得写入状态、输出或日志。
- 默认配置：`max_project_pages=1`、`close_owned_browser_on_completion=true`、`keep_open_on_needs_login=true`、`reuse_persistent_profile=true`。profile 路径与内容永不删除。
- `needs_login`、二维码、CAPTCHA或人工验证：保留项目浏览器并收敛为一个抖音登录/首页页面；状态为等待人工登录，不再显示OCR处理中。
- `success`、正常 `partial`、正常 `empty`：关闭本次页面；无其他租约时通过CDP优雅关闭项目浏览器。`failed`默认清理本次页面，只有明确需要人工浏览器处理时保留一个页面。
- 浏览器被其他项目任务租用时不得关闭实例。CDP关闭失败时，只有本任务明确启动并记录的项目根PID可做有界收尾；严禁按进程名批量结束 `chrome.exe`，严禁结束个人Chrome。
- 工作台提供“打开/复用抖音登录窗口”和可选“关闭项目抖音浏览器”。显式关闭仅在无运行任务/租约时生效。

## 范围与排除项

允许修改 `collector.py`、`trusted_news.py`、`search_collector.py`、`workbench.py`、`cli.py`、必要时 `job_runtime.py`、配置与对应测试。不得修改 `third_party/MediaCrawler`，不得读取专用 profile 内容，不得自动填充密码，不得重新真实采集或自动关闭当前真实 9223 浏览器。

## 数据与状态契约

- 持久状态只保存非敏感、项目自有的计数和租约信息：浏览器状态、租约计数、页面数量、是否由本任务启动、脱敏错误；不保存 target ID、URL、WebSocket地址或认证材料。
- 用户可见状态：`not_started`、`waiting_for_login`、`connected_ready`、`task_running`、`completed_closed`。
- 所有CDP请求、启动等待、页面关闭、浏览器退出和进程收尾均有有限超时。重复点击登录或采集不得产生多个窗口/worker。

## 测试与验收

- 单元/集成测试使用假CDP和假进程对象，覆盖：复用既有实例、并发只启动一次、页面收敛到1、before/after差集清理、needs_login保留、success/partial/empty/failed策略、其他租约保护、CDP失败/超时降级、无批量kill、无敏感输出、工作台登录按钮不采集、重复点击去重、视觉OCR音频尝试仍为0。
- 首次聚焦和首次完整pytest均在隐藏独立Windows进程组运行；测试不得连接或关闭真实9223浏览器。保持现有78项回归并增加新覆盖。
- 真实验收仅做只读CDP计数和离线模拟。本任务关闭真实窗口数必须为0；用户完成登录后再由用户触发一次真实运行，验证单页面复用、完成后自动关闭且下次启动通常保留登录态。
- 完成后执行敏感扫描、只读scheduler query、handoff更新与审计，并只追加过程记录而不读取历史。

## 停止条件

- 任何CDP、浏览器或子进程阶段超过有限预算立即降级并停止，不重试真实采集。
- 需要用户登录时停在一个项目专用页面，明确提示人工操作；绝不索取账号密码或认证材料。
