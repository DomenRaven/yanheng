# 新窗口对接 —— Phase 5 规格续作（2026-09-18）

> **给下一个 Agent / 新会话**：用户已要求 **数据后端先暂停**，在新窗口继续 **`9.16-散户决策链需求规格.md`** 相关任务。本文是「此刻状态 + 读什么 + 别碰什么」的最小对接包；环境坑与 Phase 0–4 背景仍见 `docs/HANDOFF-NOTES.md`。

---

## 1. 用户意图（一句话）

**先忽略灌库/计划任务运维**，聚焦需求规格里的 **Must 收尾（尤其 M16 trace）与 Should**，以及规格 §6 落盘/UI/验收文档；不要主动开 `run_data_update` 或恢复定时任务，除非用户明确说恢复数据后端。

---

## 2. 当前状态快照

| 领域 | 状态 |
|------|------|
| **Phase 5 Must 主链** | 代码已齐；M8 watch/hold 可核对、M1 缺 champion 失败、M4 UI 写明 T+1；探针 `scripts/probe_retail_chain.py` |
| **M16 全链路 trace** | 临时库验收通过；生产库 `paper_trades=0`、完整 trace=0（非缺字段） |
| **Should** | S1/S2/S9 **已补代码+测例**（2026-09-18）；独立周报页仍未做 |
| **灌库** | **已暂停**：`data/ingestion_pipeline_state.json` → `status: interrupted`，`run_id: 20260917-214457-3fffa3a7`，仅 `universe` 步 ok；**勿并行** resume |
| **Windows 计划任务** | 已 **Disable**（未删）：`StockQuant-WeekdayDecision` / `MidnightCatchup` / `WeekendResearch` |
| **数据策略文档** | 已写：`docs/data-maintenance-policy.md`、`docs/manuals/windows-scheduled-maintenance.md`（恢复后端时用） |

后台任务 **230665**（`run_data_update --resume`）曾被强制结束，退出码 4294967295；断点已标 `interrupted`。

---

## 3. 新窗口必读（按顺序）

1. **`docs/9.16-散户决策链需求规格.md`** — Must/Should、§8 微调、§9 阶段 A–D、**§12 自查**  
1b. **`docs/phase5-gap-construction.md`** — 缺口理论依据与工序（2026-09-18）  
2. **`docs/phase5-acceptance-report.md`** — 已验收项 + §7 投产待办  
3. **`docs/phase5-development.md`** + **`docs/phase5-constraints.md`** — 改代码边界  
4. **`docs/production-daily-runbook.md` §4** — 改 UI/建议链后的回归脚本（**非**灌库日课）

强制流程：`.cursor/rules/quant-dev-loop.mdc` + `.cursor/rules/vibe-coding-ethics.mdc`（或 skill `yanheng-dev-loop`）。闸门 **1.5** 必须在定框架前读理论库 / 更新 `docs/99-参考文献/文献与链接索引.md`。

---

## 4. 建议工作方向（规格驱动，非任务承诺）

用户转向规格续作时，常见优先级（对照 §5 / §12 / 验收报告）：

| 优先级 | 项 | 规格/验收 |
|--------|-----|-----------|
| P0 | **M16** 端到端 trace 已用临时库验收；生产库条数仍可为 0 | M16、`advice/advice_trace.py` |
| P1 | **Should** 已按序落地：S1 理由包、S2 同行开仓上限、S9 计划 vs 实际 | §5.2、`phase5-gap-construction.md` |
| P2 | §6 落盘/UI 一致性、可用性机器测试 `run_usability_machine_tests.py` | `docs/manuals/usability-test-machine.yaml` |
| 不做 | 券商 API、自动下单、改 Must 口径无 §8 理由 | Won’t |

**开发期写库**：§11 默认 — 用临时 DuckDB 或等用户恢复灌库；决策页在未就绪时会 **阻塞**（`require_warehouse_for_decisions`）。若本地库曾 ready，可只读跑 UI；若 blockers 多，先问用户是否恢复数据而非擅自 full refresh。

---

## 5. 关键路径（功能，非 ingestion）

| 能力 | 路径 |
|------|------|
| 建议引擎 | `advice/advice_engine.py`、`advice/persist_advice.py` |
| 模拟盘 | `advice/paper_broker.py`（或同目录 paper 模块） |
| 择时/待办 | `advice/entry_rules.py`、`pages/8_明日待办.py` |
| 持仓与建议 | `pages/1_持仓与建议.py` |
| 扫描 | `pages/2_掘金扫描.py` |
| 复盘 | `pages/7_历史建议复盘.py`；`advice/advice_trace.py` |
| 扫描 | `pages/2_掘金扫描.py`；`advice/reason_pack.py` |
| 行业上限 | `advice/industry_cap.py`；`pages/8_明日待办.py` |
| 投产门禁 UI | `common/ui_theme.py`（`render_production_banners`、`require_warehouse_for_decisions`） |
| 就绪（只读检查） | `common/warehouse_readiness.py`、`scripts/check_warehouse_readiness.py` |
| 规格合规脚本 | `scripts/audit_phase5_spec_compliance.py` |

启动 Streamlit（功能开发时）：

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501
```

改 Python/页面后需 **重启 Streamlit**，不能只刷新浏览器。

---

## 6. 数据后端 —— 暂停期间禁止默认动作

- **不要**跑：`run_data_update`、`run_daily_refresh`（任何 profile）、`ensure_data_fresh --apply`  
- **不要**开第二个 resume（曾出现 venv + 系统 Python 双进程）  
- **可以**只读：`check_warehouse_readiness`、`check_startup_data`（不 spawn）

用户说恢复数据后端时：

```powershell
Get-ScheduledTask StockQuant-* | Enable-ScheduledTask
cd stock-quant-system\scripts
# 续跑（单进程）
..\.venv\Scripts\python.exe -m scripts.run_data_update --resume
# 或新策略
..\.venv\Scripts\python.exe -m scripts.run_daily_refresh --profile weekday_decision
```

详见 `docs/data-maintenance-policy.md`。

---

## 7. Git / 未提交改动（交接时）

仓库大量 Phase 5 / 数据维护 / UI 文件可能仍为 **未 commit** 状态（以新窗口 `git status` 为准）。用户未要求则 **不要擅自 commit/push**；收尾时可提醒用户是否提交。

---

## 8. 上一会话已交付（便于不重复造轮子）

- 数据维护策略 + `weekday_decision` / `weekend_research` profile  
- `quotes_daily_skip_exchanges: [bj]`（日更不拖东财）  
- **2026-09-18**：日更接入 `tushare_bse_quotes`；`sync_bse_cdr_qfq_quotes(refresh_adj=True)` 写入全量 `tushare_prices` 与日更轻量步（避免旧 adj 与增量日线无交集写 0 行）；回归 `scripts/test_bse_adj_refresh.py`  
- Windows 任务安装脚本 + 本机曾注册后 **已 Disable**  
- `scripts/check_startup_data.py`  
- 规格 §12.4 / README / runbook 已链到上述文档  

---

## 9. 对接口令（给用户）

在新会话第一条消息可写：

> 读 `stock-quant-system/docs/HANDOFF-20260918-new-window.md`，按 `9.16-散户决策链需求规格.md` 继续；数据后端保持暂停。

---

*对接版本：2026-09-18。规格正文以 `9.16-散户决策链需求规格.md` 为准；本文仅描述会话切换时的工程状态。*
