# 本仓库 · 八荣八耻绑定

原文与跨项目释义以个人全局 Skill `eight-honors-eight-gates` 为准。**禁止改写八句原文。**

下面只写研衡仓库的落地，不是改写：

1. **查档求证**：akshare / tushare / lightgbm 先读官方文档或小样本真实请求。前车之鉴：`docs/phase0-acceptance-report.md` 缺口 2。
2. **对齐需求**：对照计划哪一条、验收数字是什么。
3. **请示规则**：A 股以 `research/a_share_rules.py` 或 `limit_price` / `suspend_calendar` 为准。三重障碍、Purged K-Fold 对照 `docs/03-量化方法/`。
4. **复用存量**：先搜 `common/` `research/` `ingestion/`。
5. **完备测例**：因子/模型/缺口修复必须有可复现查询或 t 检验 / p 值 / 覆盖率。
6. **恪守规范**：EAV、DuckDB 单写者、`research/` 纯函数 vs `panel.py` 唯一摸库。
7. **坦诚存疑**：样板 `docs/phase1-acceptance-report.md` 第 6 节。
8. **分步迭代**：一次一个任务/模块。
