# 单次开发指导文档：GitHub 私有仓库首版快照

日期：2026-09-09  
状态：已完成  
工作目录：`D:\work\copy_skill`

## 目标

将当前项目整理为可审查、可克隆、不会泄露本地凭据的 Git 仓库，并上传至 GitHub 账号 `hrbj18` 下的新私有仓库 `copy_skill`。

## 范围

- 纳入项目自有源码、配置模板、测试、脚本、Skill、启动器和当前开发交接文档。
- 新增面向仓库读者的 `README.md`，如实说明功能、运行方式、当前验证状态和已知限制。
- 扩充 `.gitignore`，排除密钥、Cookie、浏览器资料、模型、虚拟环境、缓存、测试临时目录、运行数据、生成物和本地过程记录。
- 将 `third_party/MediaCrawler` 作为上游 Git 子模块引用，不复制其历史、虚拟环境、浏览器资料或本地数据。
- 初始化 Git，检查暂存清单和秘密模式扫描，创建首次提交并推送到新的私有仓库。

## 安全边界

- 不读取、复制或提交 `.env.local`、Cookie 文件、浏览器用户目录和其他本地认证材料。
- 不提交 `output/`、`data/`、`.venv/`、pytest 临时目录、模型或下载素材。
- 不修改 `third_party/MediaCrawler` 上游代码。
- 不把离线测试或候选池成功描述成真实每日新闻交付成功。
- 暂不授予公开可见性；后续公开前需单独检查许可证和第三方内容边界。

## 验收标准

1. 新仓库为 `hrbj18/copy_skill`，可见性为 private，默认分支为 `main`。
2. 暂存与提交清单中没有 `.env.local`、Cookie、浏览器目录、运行数据、生成素材、模型、虚拟环境或测试缓存。
3. 对将提交文件执行秘密模式扫描，不出现真实 API key、GitHub token 或明文密码。
4. `third_party/MediaCrawler` 以 `.gitmodules` 和 gitlink 表示，指向其官方上游地址。
5. README 能说明安装、启动、核心能力、数据边界及当前实际限制。
6. 项目测试、编译检查和 handoff audit 通过，或明确记录与本次整理无关的既有失败。
7. 首次提交推送完成，远端 HEAD 与本地 HEAD 一致，Git 工作树干净。

## 实施结果

- 已创建私有仓库 `https://github.com/hrbj18/copy_skill`，默认分支为 `main`。
- 首次快照提交为 `04958e1`，包含 172 个项目文件及 MediaCrawler gitlink。
- `.env.local`、Cookie、浏览器资料、`data/`、`output/`、模型、虚拟环境、测试缓存和本地过程记录均被 `.gitignore` 排除。
- `third_party/MediaCrawler` 以子模块提交，固定在 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`。
- 对暂存文件的密钥模式扫描无命中，且没有超过 5 MB 的普通暂存文件。
- 298 项 pytest 通过，`compileall` 通过，CLI doctor 确认离线流程就绪；登录状态按设计未探测。
- README 已如实记录稳定能力、真实运行的 `partial` 状态和下一阶段适用方向。
