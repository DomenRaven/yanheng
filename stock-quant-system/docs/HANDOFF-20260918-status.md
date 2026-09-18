# 仓库状态对齐 —— 2026-09-18 夜（面板重建 + 双池冠军 + UI/说明书）

> 取代「数据后端暂停、只做规格」的旧窗口重心。灌库可按日课继续；**生产模型已分池**。

## 一句话

`feature_panel` 已重建至 **2026-08-31**；沪深 / 北交所双冠军已同面板重训并晋升；UI 与用户说明书已写明如何区分两池冠军；掘金 asof 仍为日频（约 **2026-09-17**）。

## 当前冠军（以指针为准，勿看文件夹最新）

| 池 | 指针 | run_id | 训练面板 | WF RankIC |
|----|------|--------|----------|-----------|
| hs | `champion_hs.json`（并同步 `champion.json`） | `20260918_232953` | 2016-01-29 ~ 2026-08-31 | 0.0554 |
| bj | `champion_bj.json` | `20260918_233000` | 同上 | 0.0833 |

模型二进制在 `mlops/registry/<run_id>/`（gitignore，本机保留）。远程只推指针与 `promotion_log.jsonl`。

## 必读

1. `docs/requirements-20260918-scan-pools-paper.md`（含双池重训与 E2E 留痕）
2. `docs/probe-panel-dual-retrain-live.md`（实时探针）
3. `docs/manuals/用户使用说明书.md` §「如何区分冠军」
4. `docs/production-daily-runbook.md` / `docs/data-maintenance-policy.md`

## 启动

```powershell
cd stock-quant-system
.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501
```

改代码后必须**完整重启** Streamlit，刷新浏览器不够。

## 已知局限

- hs 新冠 RankIC 略低于旧冠，按容差规则仍晋升（见需求文档）。
- 晋升门禁仍偏 RankIC，未强制 Top-N 组合收益。
- 训练月末面板 ≠ 掘金日频 asof，属设计分层。
