# 可用性测试记录（2026-09-17）

> **机器用例**：`docs/manuals/usability-test-machine.yaml`  
> **执行**：`python -m scripts.run_usability_machine_tests [--with-streamlit]`  
> **最新结果表**：`usability-test-results-latest.md`（每次跑测覆盖）

## 1. 本轮 UI 更新

| 项 | 变更 |
|----|------|
| 侧边栏 | 全页 `apply_theme` 挂载「数据状态」：可跑/阻塞、截面日、滞后、刷新按钮 |
| 首页 | 导航文案含 Phase 5（明日待办、投产检查命令） |
| 复盘页 | 增加 `render_production_banners` 与其他决策页一致 |

## 2. 机器测试执行

| 批次 | 命令 | 结果 |
|------|------|------|
| 回归 + 数据 + UI 语法 | `run_usability_machine_tests` | **全 PASS** |
| HTTP  smoke | 同上 `--with-streamlit --port 8504` | **全 PASS**（M-S01–S04） |

## 3. 测试中发现并修复的问题

| ID | 现象 | 根因 | 修复 |
|----|------|------|------|
| T-01 | M-U03 FAIL | YAML 引用不存在的 `search_symbols` | 改为 `lookup_universe(c, "紫金矿业")` |
| T-02 | `--with-streamlit` 崩溃 | 中文页面 URL 未 percent-encode | `_fetch(base, path)` 使用 `urllib.parse.quote` |
| T-03 | 投产状态分散 | 仅首页/持仓有横幅 | 侧边栏统一状态 + 复盘页补横幅 |
| T-04 | 导航未含 Phase 5 | 首页页脚文案滞后 | 更新 `app.py` 导航与 `check_warehouse_readiness` 提示 |

## 4. 仍须人工勾选的项（见 YAML `manual_only`）

- H-001：10 秒内读懂建议卡片动作与股数  
- H-002：一键练习闭环（净值变化）  
- H-003：复盘页 M15 确认成交  

## 5. 复跑命令

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m scripts.run_usability_machine_tests
.venv\Scripts\python.exe -m scripts.run_usability_machine_tests --with-streamlit
```

人工清单：`docs/manuals/人工可用性测试指南.md`（建议增补 Phase 5 步骤 2.8–2.10）。
