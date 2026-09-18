---
name: yanheng-dev-loop
description: >-
  Binds the global eight honors/eight gates skill to this A-share quant repo
  (stock-quant-system): local paths, theory library (gate 1.5), champion
  registry, A-share rules, and repo pitfalls. Use when implementing, reviewing,
  accepting, documenting, or extending tasks in this workspace (factors, models,
  ingestion, Streamlit, phase reports, manuals, Git commits). Pair with personal
  skill eight-honors-eight-gates; do not rewrite the eight sentences.
---

# 研衡绑定（本仓库）

八荣八耻与八闸门的**原文和通用顺序**以个人全局 Skill `eight-honors-eight-gates` 为准（禁止改写八句原文；闸门 **1.5** 为 2026-09-18 插入，细则见全局 `eight-gates.md`）。
本 Skill 只把闸门绑到当前 A 股量化仓库。**道德底线 > 完成速度。禁止跳步。**

通用细则仍读全局 Skill 的 `eight-honors.md` / `eight-gates.md`。
本仓库样板：`stock-quant-system/docs/phase0-acceptance-report.md` ~ `phase4-acceptance-report.md`。

## 开工前一句话

必须能说清：「对照哪条计划/验收条款、验收数字是什么」。说不清 → 先读文档，不开工。

## 本仓库闸门映射

1. **读文档**：顶层 plan 对应条款（不要编辑 plan 文件本身）+ 已有 `docs/phaseN-acceptance-report.md` + 规格/约束（如 `9.16-散户决策链需求规格.md`）。回答「对照哪条、验收数字是什么」。
1.5. **读理论**：`docs/03-量化方法/` 及相关专章（`02-经典理论/` `04-风险管理/` `07-产品设计启示/` 等）；索引 `docs/99-参考文献/文献与链接索引.md`。库中没有则期刊/图书馆外搜，原文进 `docs/99-参考文献/papers/`（gitignore）并更新索引与 `papers/MANIFEST.yaml`。体例见 `docs/00-总览/03-阅读路线图.md`「知识库维护约定」。方法缺口也可加载高质量 Skill（如 `zju-lib`）。找不到则写理论缺口。
2. **定框架**：先搜 `research/` `common/` `ingestion/` `advice/`；没有可复用实现才新建。
3. **开工实现**：一次一个模块；不做跨 Phase 大改（纯清理除外）。
4. **测试与打分**：真实数据跑 IC / RankIC / 回测 / p 值 / 覆盖率。UI 用 Playwright 或人工走完整路径。不达标回 2/3。禁止改评估口径。
5. **验收**：对照该 Phase 验收条款原文。
6. **文档与留痕**：更新验收报告 / README；模型写 `mlops/registry/<run_id>/metadata.json`。UX 打磨不新开 Phase 时写入 `docs/ux-upgrade-notes.md`。
7. **清理**：删一次性脚本、临时文件、`__pycache__`；保留 registry 与失败挑战者留痕。
8. **微调下一任务**：结果与计划不符时先改任务清单并写理由。

## 冠军-挑战者（本仓库）

新模型替换生产冠军，必须**同一套协议**（同样特征、同样 Walk-Forward、同样成本）且显著更好——**含 Top-N 组合收益，不只 RankIC**。
不显著 → 保留冠军，如实记录「新模型没有更好」。

当前冠军：`mlops/registry/champion.json`。scanner 只加载冠军。
`mlops/retrain_schedule.py` 的晋升规则必须与上述协议一致，禁止只凭 RankIC 容差放行。

## 分层与规则来源

- A 股规则：`research/a_share_rules.py`、`limit_price`、`suspend_calendar`。禁止凭印象改涨跌停/ST/T+1。
- 三重障碍 / Purged K-Fold：对照 `docs/03-量化方法/10-时序验证与标签工程.md`。
- 财务：EAV 表。DuckDB **单写者**。`research/` 纯函数；`panel.py` 是 research 里唯一摸库模块。
- UI 禁止复制业务公式；页面只调 `advice/` `risk/` `llm/`。
- 第三方接口（akshare / tushare / lightgbm）：先查档或小样本真实请求，禁止猜字段名。

## 本仓库额外禁止

- 跳过闸门 1.5 直接定框架
- 提交 `.env`、`*.duckdb`、真实 API Key
- 自动下单 / 承诺收益（产品 Won't）

## 本仓库操作坑

- PowerShell 不支持 bash heredoc。commit 信息先写文件再 `git commit -F`。
- 改 UI 后必须重启 Streamlit 进程，刷新浏览器不够。
- pandas `NaN` 是 Python 真值，兜底用 `pd.notna()`，不要 `x or ""`。
- 中文在 PowerShell 回显可能乱码，文件本身通常没坏；用 Read 工具核对。
