"""
模型漂移监控：特征PSI + 历史Walk-Forward RankIC趋势 + 生产环境样本外IC（累积到足够
交易日后才有统计意义）。

理论依据：本项目 `.cursor/rules/quant-dev-loop.mdc` 记录的CD4ML/对冲基金MLOps实践
（PSI>0.2/0.25触发重训、feature/prediction/performance三层监控）。

**如实说明当前局限（对齐vibe coding八荣八耻第7条）**：
- "生产环境样本外IC"依赖 `prediction_log` 表逐日累积的历史打分记录，本项目
  `advice/scanner.py` 目前只运行过很少的交易日，暂时不足以算出有统计意义的漂移曲线；
  `compute_realized_ic()` 在数据不足时会明确返回"数据不足"而不是编造一个数字。
- "训练基线分布"不是从模型训练时刻真正冻结保存的分布（当前 `research/train_lightgbm.py`
  没有存这个），而是用 `data/feature_panel.parquet`（训练所用同一份面板）里冠军模型
  `date_range` 覆盖的截面近似重建，足够做PSI监控但不是逐字节可复现的训练快照。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("mlops.drift_monitor")

_PSI_BINS = 10
_PSI_WARN = 0.10
_PSI_ALERT = 0.25


def _read_champion_metadata(registry_dir: str = "mlops/registry") -> dict:
    registry_path = Path(registry_dir)
    champion_file = registry_path / "champion.json"
    if not champion_file.exists():
        raise FileNotFoundError("没有 champion.json，先跑 mlops/retrain_schedule.py --bootstrap 或做一次正式重训")
    with open(champion_file, encoding="utf-8") as f:
        champion = json.load(f)
    with open(registry_path / champion["run_id"] / "metadata.json", encoding="utf-8") as f:
        metadata = json.load(f)
    return metadata


def _psi_for_series(ref: pd.Series, cur: pd.Series, bins: int = _PSI_BINS) -> float:
    ref = ref.dropna()
    cur = cur.dropna()
    if len(ref) < 30 or len(cur) < 30:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)
    ref_pct = np.clip(ref_counts / max(ref_counts.sum(), 1), 1e-4, None)
    cur_pct = np.clip(cur_counts / max(cur_counts.sum(), 1), 1e-4, None)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_feature_psi(panel_path: str = "data/feature_panel.parquet", registry_dir: str = "mlops/registry") -> pd.DataFrame:
    """训练基线分布(面板最后6个月截面) vs 当前快照分布，逐特征算PSI。"""
    from research.panel import build_asof_snapshot

    metadata = _read_champion_metadata(registry_dir)
    feature_cols = metadata["feature_cols"]

    panel = pd.read_parquet(panel_path)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    cutoff = panel["trade_date"].max() - pd.Timedelta(days=180)
    reference = panel[panel["trade_date"] >= cutoff]

    current = build_asof_snapshot()

    rows = []
    for col in feature_cols:
        if col not in reference.columns or col not in current.columns:
            continue
        psi = _psi_for_series(reference[col], current[col])
        if np.isnan(psi):
            level = "数据不足"
        elif psi < _PSI_WARN:
            level = "稳定"
        elif psi < _PSI_ALERT:
            level = "中度漂移(建议关注)"
        else:
            level = "显著漂移(建议触发重训)"
        rows.append({"feature": col, "psi": round(psi, 4) if not np.isnan(psi) else None, "level": level})
    return pd.DataFrame(rows).sort_values("psi", ascending=False, na_position="last")


def summarize_walk_forward_trend(registry_dir: str = "mlops/registry") -> dict:
    """冠军模型自身训练时的walk-forward各折RankIC是否有随时间衰减的迹象
    （折是按时间展开窗口顺序排列的，不是随机的，见 research/train_lightgbm.py）。"""
    metadata = _read_champion_metadata(registry_dir)
    folds = metadata.get("walk_forward_fold_detail", [])
    if len(folds) < 3:
        return {"trend": "折数不足，无法判断趋势"}
    ics = [f["rank_ic_mean"] for f in folds]
    first_half = np.mean(ics[: len(ics) // 2])
    second_half = np.mean(ics[len(ics) // 2:])
    decay = first_half - second_half
    if decay > 0.03:
        trend = f"前半段RankIC({first_half:.4f}) 明显高于后半段({second_half:.4f})，存在衰减迹象"
    elif decay < -0.03:
        trend = f"后半段RankIC({second_half:.4f}) 高于前半段({first_half:.4f})，近期表现更好，无衰减迹象"
    else:
        trend = f"前后半段RankIC接近(前{first_half:.4f}/后{second_half:.4f})，走查稳定"
    return {"fold_rank_ics": ics, "first_half_mean": round(float(first_half), 4),
            "second_half_mean": round(float(second_half), 4), "trend": trend}


def compute_rolling_sharpe(
    backtest_returns_path: str = "docs/backtest_returns.csv",
    window: int = 6,
) -> dict:
    """基于Phase1 `research/backtest.py` 产出的月度净收益序列算滚动夏普（对齐计划文档
    Phase2验收条款明确要求的"滚动夏普"这一项）。这是Walk-Forward历史回测期内的滚动
    夏普，不是"实盘滚动夏普"——本系统还没有累积实盘/模拟盘的逐月收益序列，等
    `prediction_log` 累积够数据后可以换成真正的生产滚动夏普，接口不需要变。"""
    path = Path(backtest_returns_path)
    if not path.exists():
        return {"status": "missing", "message": f"{backtest_returns_path} 不存在，先跑 research/backtest.py"}
    bt = pd.read_csv(path)
    if len(bt) < window:
        return {"status": "insufficient_history", "message": f"回测期只有{len(bt)}期，不足以算{window}期滚动夏普"}
    periods_per_year = 12  # 月度调仓
    roll_mean = bt["net_return"].rolling(window).mean()
    roll_std = bt["net_return"].rolling(window).std()
    rolling_sharpe = (roll_mean * periods_per_year) / (roll_std * np.sqrt(periods_per_year))
    out = pd.DataFrame({"trade_date": bt["trade_date"], "rolling_sharpe": rolling_sharpe}).dropna()
    latest = float(out["rolling_sharpe"].iloc[-1]) if not out.empty else None
    worst = float(out["rolling_sharpe"].min()) if not out.empty else None
    return {
        "status": "ok",
        "window": window,
        "latest_rolling_sharpe": round(latest, 3) if latest is not None else None,
        "worst_rolling_sharpe": round(worst, 3) if worst is not None else None,
        "series_tail": out.tail(6).assign(
            trade_date=lambda d: d["trade_date"].astype(str),
            rolling_sharpe=lambda d: d["rolling_sharpe"].round(3),
        ).to_dict(orient="records"),
    }


def compute_realized_ic(min_trading_days: int = 20, forward_days: int = 20) -> dict:
    """用 prediction_log 里累积的历史打分，对齐 forward_days 后的真实收益，算样本外RankIC。
    数据不够时明确说"数据不足"，不编造数字（对齐vibe coding八荣八耻第7条）。"""
    from common.db import get_connection

    conn = get_connection()
    try:
        n_days = conn.execute("SELECT count(distinct trade_date) FROM prediction_log").fetchone()[0]
        if n_days < min_trading_days:
            return {
                "status": "insufficient_history",
                "message": f"prediction_log 只累积了{n_days}个交易日的打分记录，"
                           f"至少需要{min_trading_days}个交易日(且每条记录要能对齐{forward_days}个交易日后的"
                           f"真实收益)才能算出有统计意义的样本外漂移曲线，暂不出具漂移结论。",
            }
        df = conn.execute(
            f"""
            WITH fwd AS (
                SELECT symbol, trade_date,
                       lead(close, {forward_days}) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_fwd,
                       close
                FROM daily_quotes WHERE adjust = 'qfq'
            )
            SELECT p.trade_date, p.symbol, p.pred_score, (f.close_fwd / f.close - 1) AS fwd_return
            FROM prediction_log p
            JOIN fwd f ON f.symbol = p.symbol AND f.trade_date = p.trade_date
            WHERE f.close_fwd IS NOT NULL
            """
        ).df()
    finally:
        conn.close()

    if df.empty:
        return {"status": "insufficient_history", "message": "打分记录都还不能对齐足够交易日之后的真实收益，暂不出具结论"}

    ics = []
    for date, grp in df.groupby("trade_date"):
        if len(grp) < 30:
            continue
        ic = grp["pred_score"].corr(grp["fwd_return"], method="spearman")
        if pd.notna(ic):
            ics.append(ic)
    if not ics:
        return {"status": "insufficient_history", "message": "有对齐的记录但每日候选数不足30，暂不出具结论"}
    return {
        "status": "ok",
        "n_periods": len(ics),
        "rank_ic_mean": round(float(np.mean(ics)), 4),
        "rank_ic_std": round(float(np.std(ics)), 4),
    }


def generate_drift_report(panel_path: str = "data/feature_panel.parquet", registry_dir: str = "mlops/registry") -> dict:
    psi_df = compute_feature_psi(panel_path, registry_dir)
    wf_trend = summarize_walk_forward_trend(registry_dir)
    realized = compute_realized_ic()
    rolling_sharpe = compute_rolling_sharpe()
    report = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "feature_psi": psi_df.to_dict(orient="records"),
        "walk_forward_trend": wf_trend,
        "realized_ic_since_champion": realized,
        "rolling_sharpe_backtest": rolling_sharpe,
    }
    n_alert = (psi_df["level"] == "显著漂移(建议触发重训)").sum()
    n_warn = (psi_df["level"] == "中度漂移(建议关注)").sum()
    report["overall_flag"] = (
        "建议重训" if n_alert > 0 else ("建议关注" if n_warn > 0 else "正常")
    )
    return report


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = generate_drift_report()
    print("=== 特征PSI ===")
    print(pd.DataFrame(report["feature_psi"]).to_string(index=False))
    print("\n=== Walk-Forward趋势 ===")
    print(report["walk_forward_trend"]["trend"])
    print("\n=== 生产环境样本外IC ===")
    print(report["realized_ic_since_champion"])
    print("\n=== 回测期滚动夏普(月度调仓，窗口=6) ===")
    print(report["rolling_sharpe_backtest"])
    print("\n=== 总体结论 ===", report["overall_flag"])

    out_path = Path("mlops/registry") / "latest_drift_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 {out_path}")
