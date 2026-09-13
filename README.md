# copy_skill

面向科技短视频生产的本地内容情报与素材整理工具。项目能够采集抖音账号和关键词的公开作品信息，按原始互动数据形成关注度排序，合并相近事件，并尝试通过公开网页、视频画面文字和模型生成补充事件内容。它还可以生成按日期组织的 Markdown/JSON 素材包，供 OpenMontage 等下游项目读取。

## 当前能力

- 采集配置账号及关键词的抖音作品元数据。
- 保存原始互动量，生成热度排序和独立的编辑推荐排序。
- 合并同一事件的多个视频，并保留来源记录。
- 对无主播、以画面文字为主的视频进行有限抽帧和 OCR。
- 从受限公开新闻源和固定查询矩阵发现科技事件线索。
- 为少量已选新闻查找视觉素材，并输出每日素材交换包。
- 提供 Windows 图形工作台和命令行入口。

抖音数据只用于发现选题和观察传播情况，不能证明新闻内容真实。公开网页搜索结果同样只是线索；面向发布的事实仍需由下游编辑核验。

## 当前状态

项目代码和自动化测试已经覆盖采集、聚类、排序、内容补全、素材输出、浏览器复用及 Windows 进程安全等主要路径。

真实每日运行仍可能得到 `partial`。最近一次完整诊断中，公开文章正文请求大面积超时，模型整理调用失败，严格的人读新闻卡片数量为零。因此当前项目更适合作为“抖音关注度采集、指定新闻素材整理和下游交接工具”，暂不应把自动生成的每日新闻榜视为稳定成品。

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- Git
- 可用的 Chromium/Chrome 浏览器
- `uv`，推荐用于安装 Python 依赖

MediaCrawler 作为 Git 子模块引用。首次克隆请使用：

```powershell
git clone --recurse-submodules https://github.com/hrbj18/copy_skill.git
cd copy_skill
uv sync --dev
```

缺少子模块时可执行：

```powershell
git submodule update --init --recursive
```

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
