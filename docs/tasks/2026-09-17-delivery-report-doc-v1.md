# 交付汇报文档（delivery report doc）v1

创建：2026-09-17。状态：已实现。

## 目标

让「每期交付都有一份人读的汇报文档」从**一次性人工补写**变成**每次工作流程的一部分**，且事实层由工具生成、结论层由 agent 撰写，两者不互相覆盖。

## 背景（为什么需要）

2026-09-17 排查发现：整个 `output/` 下 30+ 期交付目录、70 份 Markdown，只有三类文件，全部由管线自动生成 ——
`00-交付说明.md`（每期必有）、`01-脚本思路/脚本思路.md`、`04-原片/原片索引.md`。在 `src/` 内检索「汇报」命中 0 处。

即：**管线产出的是给机器读的账，给人读的汇报一直是缺的**。缺它时表现为「东西在、但看不到」——
用户连续三次在交付目录里找不到本该存在的东西（04-原片、汇报文档、两处 output 的归属）。

## 范围

新增：

- `scripts/build_report_doc.py` —— 生成 / 刷新 `汇报文档.md`。
- `tests/test_report_doc.py` —— 7 个用例（含 CLI 退出码与人工段保留）。

修改（契约层，仅追加，不改既有语义）：

- `AGENTS.md` 新增 `Delivery documentation (mandatory)` 一节。
- `docs/handoff/README.md` 路由末尾指向该节。
- `docs/handoff/CODE_MAP.md` 注册命令与测试文件。
- `docs/handoff/PRODUCT_RULES.md` 在复刻目录条目追加「每个目录带 `汇报文档.md`」。

## 设计决策

1. **事实与结论分离**：自动段（一/四/五/七）每次运行重写；人工段（二 产品是什么、三 拆解要点、六 风险与待办）按**中文序号**匹配并原样保留。
   ⇒ 重跑刷新数字，不会抹掉已写好的分析。`--force` 是唯一的丢弃入口。
2. **`--check` 作为闸门**：人工段仍带 `<!-- HUMAN-TODO -->` 时退出码 2。
   ⇒ 「有没有写」可被脚本判定，不依赖人记得。
3. **体积口径必须取 `material_replica.delivered_bytes`**，不得取 `download_budget.used.delivered_bytes`。
   后者是**下载流量**（含后来被否掉的素材）。实机复现过两种读数：入选 21.7 MiB（未达标）vs 下载 72.9 MiB（看起来达标）——取错字段会把 `insufficient` 藏起来。
4. **上下限从 `清单.json` 读**（`material_replica.min/max_delivered_bytes`、`delivery_folder.max_delivery_folder_bytes`），不硬编码。
5. **缺 `清单.json` 不报错**：人工整理包按「无采集链账本」降级渲染，只做目录清点。
6. **两套体积都报**：「交付目录当前实测」与「管线交付时刻记录」并列，差额标为人工增补。实机差额 21.7 MiB（手补的 04-原片 4 条）。

## 验收

- `pytest tests/test_report_doc.py tests/test_handoff.py` → 12 passed。
- `scripts/audit_handoff.py --root .` → 7 项全 OK（`AGENTS.md` 3896/8000、`PRODUCT_RULES.md` 4932/5000）。
- 实机端到端（`output/复刻视频/9.17智元远征A3 Ultra复刻视频/`）：
  - 首跑修正了错误的体积口径（72.9 → 21.7 MiB）；
  - 人工段二/三/六在二次、三次重跑后逐字保留；
  - `--check` 在人工段留空时退出 2、填完后退出 0。

## 排除

- 不改 `src/`，不改管线产物结构，不动 `direct_delivery` / 闸门配置。
- 不把该文档塞进 `CURRENT_STATUS.md`：它只剩 43 字符额度（2957/3000），而规则属于流程契约、不属于状态事实，正确的家是 `AGENTS.md`。
- 不自动把历史 30+ 期补写汇报；只保证**今后每一期**必出。

## 已知边界

- `汇报文档.md` 是 `output/` 内的交付物，`--overwrite` 重跑会随目录重建；需重新生成。
- 管线告警文案自身有缺陷：本期告警称「不足最小 2 条」而实际入选 4 条（疑似 `min` 取错字段）。本任务不修，已在汇报文档第六节标注。
