# 本仓库 · 八闸门映射

通用细则以个人全局 Skill `eight-honors-eight-gates` 为准。本文件只映射到研衡路径。

## 闸门 1

顶层计划文档对应 Phase 条款（`个人炒股辅助本地应用_76077b1a.plan.md`，**不要编辑该 plan 文件本身**）、已有 `stock-quant-system/docs/phaseN-acceptance-report.md`、规格与约束（如 `9.16-散户决策链需求规格.md`、`phase5-constraints.md`）。

本闸门只对齐需求与已有工程结论，不在这里用理论代替验收条款。

## 闸门 1.5

读完需求后、定框架前：读仓库理论库作为后续指导。

| 先读存量 | 路径 |
|----------|------|
| 方法 | 仓库根 `docs/03-量化方法/`（Purged K-Fold / 日频执行等） |
| 经典与行为 | `docs/02-经典理论/` |
| 风控与仓位 | `docs/04-风险管理/` |
| 产品 Schema | `docs/07-产品设计启示/` |
| 索引 | `docs/99-参考文献/文献与链接索引.md` |
| 样板 | `docs/00-总览/08-第四轮散户决策链补齐对照.md`（检索→清洗→专章→索引） |

库中没有：期刊 / 浙大 Summon / 万方 / 作者主页；社区只作定性。有原文尽量下载到 `docs/99-参考文献/papers/`（**不要 git add PDF**），摘录进 `docs/99-参考文献/notes/`，更新 `papers/README.md` 与 `MANIFEST.yaml`。

清洗：`%PDF` 魔数、首页题名、附录不当正文、机翻只作对照。分类与格式：`docs/00-总览/03-阅读路线图.md`「知识库维护约定」（文首一句话、边界、产品启示）。完成后更新文献索引；新开专章时补阅读路线。

方法类缺口也可使用已安装的高质量 Skill（如 `zju-lib` 查馆藏）。找不到权威来源 → 写「理论缺口」，闸门 2 标成假设。

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
