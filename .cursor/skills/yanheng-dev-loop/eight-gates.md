# 本仓库 · 八闸门映射

通用细则以个人全局 Skill `eight-honors-eight-gates` 为准。本文件只映射到研衡路径。

## 闸门 1

顶层计划文档对应 Phase 条款（`个人炒股辅助本地应用_76077b1a.plan.md`，**不要编辑该 plan 文件本身**）、`docs/03-量化方法/` 相关篇、已有 `stock-quant-system/docs/phaseN-acceptance-report.md`。

## 闸门 2

优先复用 `research/`、`common/`、`ingestion/`。非平凡选型写一句「为什么选这个、放弃了什么」。

## 闸门 3

一次一个模块。不做跨多个 Phase 的一次性大改（纯清理除外）。

## 闸门 4

真实数据：IC / RankIC / 回测 / p 值 / 覆盖率。「能跑」不算过。UI：Playwright 或人工走完整路径。

晋升生产冠军时必须含 Top-N 组合收益，不能只看 RankIC（Phase 3：DL RankIC 更好但 Top-30 更差，未替换）。

## 闸门 5

对照该 Phase 验收条款原文。不满足则修，或写清数据源上限/工程决策。

## 闸门 6

`docs/phaseN-acceptance-report.md`、README、`mlops/registry/<run_id>/metadata.json`。UX 打磨不新开 Phase 时写入 `docs/ux-upgrade-notes.md`。

## 闸门 7

删一次性脚本、临时文件、`__pycache__`。保留 registry、失败挑战者、验收报告。

## 闸门 8

结果与计划不符时，先改任务清单和验收标准并写理由。
