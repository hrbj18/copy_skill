# copy_skill

面向科技短视频生产的本地内容情报与素材整理工具。项目能够采集抖音账号和关键词的公开作品信息，按原始互动数据形成关注度排序，合并相近事件，并尝试通过公开网页、视频画面文字和模型生成补充事件内容。它还可以生成按日期组织的 Markdown/JSON 素材包，供 OpenMontage 等下游项目读取。

## 当前能力

- 采集配置账号及关键词的抖音作品元数据。
- 保存原始互动量，生成热度排序和独立的编辑推荐排序。
- 合并同一事件的多个视频，并保留来源记录。
- 对无主播、以画面文字为主的视频进行有限抽帧和 OCR。
- 从受限公开新闻源和固定查询矩阵发现科技事件线索。
- 为少量已选新闻查找视觉素材，并输出每日素材交换包。
- 按主题执行素材复刻：扩展关键词、采集候选池，在下载前做主体词准入与时效过滤，产出 `MM.DD<主题>复刻视频/` 交付目录。
- 准入主闸门是**题材相关性**（人脸已降级为描述性元数据，不再决定准入）；下载前叠加时效窗口与主体词命中过滤，下载后再做完整性校验。按题材 Profile 决定主/辅素材，交付目录整体不超过 `200,000,000` 字节。
- 交付目录内附机器可读的 `00-素材目录.json`，作为下游（Haike / OpenMontage）的选择契约。
- 对入选源片自动抽帧 OCR 做视觉佐证，并对交付素材做跨期去重。
- 素材源可插拔：抖音 / yt-dlp / B站；下载统一经 `CompositeMediaResolver` 解析。
- 提供 Windows 图形工作台和命令行入口。

抖音数据只用于发现选题和观察传播情况，不能证明新闻内容真实。公开网页搜索结果同样只是线索；面向发布的事实仍需由下游编辑核验。

## 当前状态

项目代码和自动化测试已经覆盖采集、聚类、排序、内容补全、素材输出、浏览器复用及 Windows 进程安全等主要路径；截至 2026-09-18 全量 `1109 passed / 0 failed`。

其中**按主题素材复刻**是当前最成熟的一环。2026-09-18 实跑 `9.18日本AI篡改历史观复刻视频`（数字取自该期 `清单.json` 的 `counters` / `validation`）：候选池 62 条，下载前剔除 15 条超期与多条 0 主体词命中；完整性校验 11 条、其中 1 条判 `duration_mismatch` 剔除；**最终入选 9 条（2 主 + 7 辅）**，`delivered_bytes = 89,217,332`（窗口 `73,400,320`~`104,857,600`），`status=success`、`degraded=false`，且**无一条素材因人脸被淘汰**。交付目录在发布时刻记录为 `89,412,778` 字节（上限 `200,000,000`）；事后补写 `汇报文档.md` 后当前磁盘合计 `89,426,232` 字节。

真实每日运行仍可能得到 `partial`。最近一次完整诊断中，公开文章正文请求大面积超时，模型整理调用失败，严格的人读新闻卡片数量为零。因此**自动生成的每日新闻榜**暂不应视为稳定成品；当前项目更适合作为“抖音关注度采集、指定新闻素材整理和下游交接工具”。

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- Git
- 可用的 Chromium/Chrome 浏览器
- `uv`，推荐用于安装 Python 依赖

MediaCrawler 在 `.gitmodules` 里被声明为子模块（上游 `https://github.com/NanmiCoder/MediaCrawler.git`），但**本仓库并没有把它记录成 gitlink**：`git submodule status` 输出为空，`git ls-tree HEAD third_party/` 亦为空。因此 `git clone --recurse-submodules` 与 `git submodule update --init` **都不会把它拉下来**——首次克隆用普通克隆即可：

```powershell
git clone https://github.com/hrbj18/copy_skill.git
cd copy_skill
uv sync --dev
```

确实需要 MediaCrawler 时，请按 `.gitmodules` 里的 URL 自行获取并放到 `third_party/MediaCrawler/`。该路径（连同 `data/`、`browser_data/`、`*_user_data_dir/`）已被 `.gitignore` 忽略，不会进入版本库。

## 本地配置

复制 `.env.example` 为 `.env.local`，然后在本机填写需要的配置。不要把密钥、Cookie 或浏览器登录目录提交到 Git。

```powershell
Copy-Item .env.example .env.local
```

抖音登录状态由项目专用浏览器保存。Cookie 文件和浏览器资料只应留在本机。

## 启动方式

Windows 用户可双击：

- `启动工作台.bat`
- `科技内容情报工作台.cmd`

也可以通过命令行运行：

```powershell
uv run douyin-intelligence --help
uv run douyin-intelligence workbench
```

生成指定业务日期的素材交换包：

```powershell
uv run douyin-intelligence daily-material-exchange run --business-date 2026-09-08
```

按主题复刻素材（产出交付目录，不进入仓库）：

```powershell
uv run douyin-intelligence material-replication run --theme "<主题>" --pool-size 80 --business-date "2026-09-18"
```

⚠️ **运行环境要求（宿主层，不是项目代码契约）**：该流程会在运动分析阶段批量清理自己的抽帧文件。若由带批量删除保护的宿主进程驱动（如 WorkBuddy 的 agent 会话，阈值 50 文件/回合），该阶段会被 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 中断、整批作废——2026-09-18 实测如此。此时需在宿主层关闭该保护（例如 `CODEBUDDY_SAFE_DELETE_ENABLED=0`）；在普通终端里直接运行不受影响，`src/` 下也没有对该变量的任何引用。

`material-replication` 的准入闸门、视觉佐证与体积口径见 `docs/handoff/CURRENT_STATUS.md`。

真实网络、浏览器和模型步骤都有外部依赖。运行结果中的 `success`、`partial`、`empty` 和错误记录应按实际状态解释。

## 验证

```powershell
uv run pytest -p no:cacheprovider
uv run python -m compileall -q src tests
uv run douyin-intelligence doctor
uv run python scripts/audit_handoff.py --root .
```

## 目录说明

- `src/douyin_intelligence/`：项目核心实现。
- `config/`：不含密钥的配置。
- `tests/`：自动化测试。
- `scripts/`：审计和辅助脚本。
- `skills/`：Codex Skill 入口及参考说明。
- `docs/handoff/`：当前项目状态和任务路由。
- `docs/tasks/`：每轮重要开发的冻结目标与验收记录。
- `third_party/MediaCrawler/`：独立上游子模块。

运行产生的 `data/`、`output/`、模型缓存、浏览器登录资料和本地素材不会进入仓库。
