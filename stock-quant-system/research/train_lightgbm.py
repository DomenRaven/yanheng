"""
三重障碍标签 + Purged K-Fold / Walk-Forward 训练 LightGBM 排序模型。

标签：对每只股票的日线qfq收盘价跑 research/labeling.triple_barrier
（止盈/止损/最大持有期=20个交易日，约等于月度再平衡的下一个截面），
取"月末截面日当天"对应的标签，与 research/panel.py 产出的月度因子面板对齐
——标签构造用的是截面日之后最多20个交易日的价格路径，因子构造用的是截面日
及之前的信息，时间箭头正确，不构成未来函数。

模型：LightGBM LambdaRank（lambdarank目标函数是"排序模型"最直接对应的LightGBM实现），
按每个截面日分组（group），相关性(relevance)取三重障碍标签 {-1,0,1} 映射到 {0,1,2}。

验证：Purged K-Fold（按截面日期切分，而不是按行切分——同一天的几千只股票必须在
同一折，否则会把同一天内的截面相关性泄漏到训练/测试两侧）+ Walk-Forward
两种切分都跑，输出各折测试集上的 RankIC，作为样本外表现的主要验收指标
（对齐计划文档"头尾组统计显著收益差"的验收标准，头尾组分组回测见 factor_eval 同款方法，
应用到模型预测分数上而非单因子上，在 advice/scanner.py 里复用）。

产出：mlops/registry/<run_id>/model.pkl + metadata.json（简单版本化，完整的
漂移监控/定期重训留给 Phase 2）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from research.factor_eval import FACTOR_COLS, add_forward_return
from research.validation import purged_kfold_splits, walk_forward_splits

logger = logging.getLogger("research.train_lightgbm")

_LABEL_HORIZON = 20  # 交易日，约等于1个月，与月度再平衡截面对齐


def attach_triple_barrier_labels(
    panel: pd.DataFrame,
    profit_take: float = 0.08,
    stop_loss: float = 0.08,
    max_holding: int = _LABEL_HORIZON,
) -> pd.DataFrame:
    """给面板每一行(symbol, trade_date)附加三重障碍标签。逐股票跑一次
    triple_barrier（对该股票全部历史算一遍，内部用向量化滚动波动率，很快），
    再按trade_date取值对齐到面板行——比对每个截面日单独截取窗口跑一次快得多。"""
    from common.db import get_connection
    from research.labeling import triple_barrier

    symbols = panel["symbol"].unique().tolist()
    conn = get_connection()
    try:
        placeholders = ",".join(["?"] * len(symbols))
        daily = conn.execute(
            f"""
            SELECT symbol, trade_date, close FROM daily_quotes
            WHERE adjust='qfq' AND symbol IN ({placeholders})
            ORDER BY symbol, trade_date
            """,
            symbols,
        ).df()
    finally:
        conn.close()

    label_frames = []
    for symbol, grp in daily.groupby("symbol", sort=False):
        close = grp.set_index("trade_date")["close"]
        lab = triple_barrier(
            close, profit_take=profit_take, stop_loss=stop_loss, max_holding=max_holding
        )
        lab["symbol"] = symbol
        lab["trade_date"] = lab.index
        label_frames.append(lab.reset_index(drop=True))
    labels = pd.concat(label_frames, ignore_index=True)
    labels["trade_date"] = pd.to_datetime(labels["trade_date"]).dt.date

    out = panel.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out = out.merge(labels, on=["symbol", "trade_date"], how="left")
    return out


def _date_level_splits(dates: np.ndarray, mode: str, **kwargs):
    """purged_kfold_splits/walk_forward_splits 是按"样本序号"切分的通用工具，
    这里的"样本"是唯一截面日期（而不是行），保证同一天的所有股票被分进同一折。"""
    n = len(dates)
    if mode == "purged_kfold":
        splitter = purged_kfold_splits(n, **kwargs)
    elif mode == "walk_forward":
        splitter = walk_forward_splits(n, **kwargs)
    else:
        raise ValueError(f"未知切分模式: {mode}")
    for train_idx, test_idx in splitter:
        yield dates[train_idx], dates[test_idx]


def _build_xy(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    cols = [c for c in FACTOR_COLS if c in df.columns]
    X = df[cols].copy()
    for c in cols:
        X[c] = X[c].clip(X[c].quantile(0.01), X[c].quantile(0.99))
    y_relevance = (df["label"] + 1).astype(int)  # {-1,0,1} -> {0,1,2}
    return X, y_relevance, cols


def _train_one_fold(train_df: pd.DataFrame, test_df: pd.DataFrame) -> tuple[lgb.LGBMRanker, pd.DataFrame]:
    # LightGBM 的 group 参数要求：group里第k个数字对应X里连续的第k段行，
    # 所以必须先按 trade_date 排序，再切X/y，否则group分组会跟行错位。
    train_df_sorted = train_df.sort_values("trade_date")
    test_df_sorted = test_df.sort_values("trade_date")
    train_group = train_df_sorted.groupby("trade_date", sort=True).size().to_numpy()
    X_train, y_train, cols = _build_xy(train_df_sorted)
    X_test, y_test, _ = _build_xy(test_df_sorted)

    model = lgb.LGBMRanker(
        objective="lambdarank",
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=50,
        random_state=42,
        verbosity=-1,
    )
    model.fit(X_train, y_train, group=train_group)

    test_df_sorted = test_df_sorted.copy()
    test_df_sorted["pred_score"] = model.predict(X_test)
    return model, test_df_sorted


def evaluate_fold(test_df: pd.DataFrame) -> dict:
    """折内样本外 RankIC：预测分数 vs 实际下期收益（复用 factor_eval 的口径）。"""
    from scipy import stats

    rows = []
    for date, grp in test_df.groupby("trade_date"):
        sub = grp[["pred_score", "fwd_return"]].dropna()
        if len(sub) < 30:
            continue
        rank_ic, _ = stats.spearmanr(sub["pred_score"], sub["fwd_return"])
        rows.append(rank_ic)
    if not rows:
        return {"rank_ic_mean": np.nan, "n_periods": 0}
    arr = np.array(rows)
    return {
        "rank_ic_mean": round(float(arr.mean()), 4),
        "rank_ic_ir": round(float(arr.mean() / arr.std()), 3) if arr.std() else np.nan,
        "pct_positive": round(float((arr > 0).mean()), 3),
        "n_periods": len(rows),
    }


def walk_forward_oos_scores(
    panel: pd.DataFrame,
    train_size: int | None = None,
    test_size: int | None = None,
) -> pd.DataFrame:
    """给 research/backtest.py 用：把全部 walk-forward 折的测试集预测结果拼接成
    一条连续的"样本外预测分数"时间序列（覆盖除首个训练窗口外的绝大部分历史），
    每一段都只用该时点"当时能看到"的历史数据训练——这是回测能成立的前提。"""
    panel = panel.copy()
    if "label" not in panel.columns:
        panel = attach_triple_barrier_labels(panel)
    panel = panel.dropna(subset=["label"])

    unique_dates = np.array(sorted(panel["trade_date"].unique()))
    n_dates = len(unique_dates)
    train_size = train_size or max(24, n_dates // 3)
    test_size = test_size or max(6, n_dates // 10)

    oos_frames = []
    for train_dates, test_dates in _date_level_splits(
        unique_dates, "walk_forward", train_size=train_size, test_size=test_size, expanding=True
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)]
        test_df = panel[panel["trade_date"].isin(test_dates)]
        if train_df.empty or test_df.empty:
            continue
        _, test_pred = _train_one_fold(train_df, test_df)
        oos_frames.append(test_pred)

    if not oos_frames:
        return pd.DataFrame()
    return pd.concat(oos_frames, ignore_index=True)


def run_training(
    panel_path: str = "data/feature_panel.parquet",
    registry_dir: str = "mlops/registry",
    *,
    pool_id: str | None = None,
) -> dict:
    panel = pd.read_parquet(panel_path)
    if pool_id:
        from advice.champion_registry import POOL_BJ, POOL_HS

        if "exchange" not in panel.columns:
            raise ValueError("面板缺少 exchange 列，无法分池训练")
        before = len(panel)
        if pool_id == POOL_HS:
            panel = panel[panel["exchange"].isin(["sh", "sz"])].copy()
        elif pool_id == POOL_BJ:
            panel = panel[panel["exchange"] == "bj"].copy()
        else:
            raise ValueError(f"未知 pool_id={pool_id}")
        logger.info("分池训练 pool=%s：%d → %d 行", pool_id, before, len(panel))
        if panel.empty:
            raise ValueError(f"池 {pool_id} 训练面板为空")
    panel = add_forward_return(panel)
    panel = attach_triple_barrier_labels(panel)
    panel = panel.dropna(subset=["label"])

    unique_dates = np.array(sorted(panel["trade_date"].unique()))
    logger.info("面板截面日数: %d，总行数: %d", len(unique_dates), len(panel))

    fold_results = {"purged_kfold": [], "walk_forward": []}

    for train_dates, test_dates in _date_level_splits(
        unique_dates, "purged_kfold", n_splits=5, label_horizon=1, embargo=1
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)]
        test_df = panel[panel["trade_date"].isin(test_dates)]
        if train_df.empty or test_df.empty:
            continue
        _, test_pred = _train_one_fold(train_df, test_df)
        fold_results["purged_kfold"].append(evaluate_fold(test_pred))

    n_dates = len(unique_dates)
    train_size = max(24, n_dates // 3)
    test_size = max(6, n_dates // 10)
    for train_dates, test_dates in _date_level_splits(
        unique_dates, "walk_forward", train_size=train_size, test_size=test_size, expanding=True
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)]
        test_df = panel[panel["trade_date"].isin(test_dates)]
        if train_df.empty or test_df.empty:
            continue
        _, test_pred = _train_one_fold(train_df, test_df)
        fold_results["walk_forward"].append(evaluate_fold(test_pred))

    # 最终生产模型：用全部历史数据训练（walk-forward/purged-kfold只用来估计样本外表现）
    full_group = panel.sort_values("trade_date").groupby("trade_date", sort=True).size().to_numpy()
    panel_sorted = panel.sort_values("trade_date")
    X_full, y_full, feature_cols = _build_xy(panel_sorted)
    final_model = lgb.LGBMRanker(
        objective="lambdarank",
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=50,
        random_state=42,
        verbosity=-1,
    )
    final_model.fit(X_full, y_full, group=full_group)

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(registry_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "model.pkl", "wb") as f:
        pickle.dump(final_model, f)

    def _agg(folds: list[dict]) -> dict:
        valid = [f for f in folds if f.get("n_periods", 0) > 0]
        if not valid:
            return {"n_folds": 0}
        return {
            "n_folds": len(valid),
            "rank_ic_mean": round(float(np.mean([f["rank_ic_mean"] for f in valid])), 4),
            "pct_positive_mean": round(float(np.mean([f["pct_positive"] for f in valid])), 3),
        }

    metadata = {
        "run_id": run_id,
        "trained_at": dt.datetime.now().isoformat(),
        "pool_id": pool_id,
        "feature_cols": feature_cols,
        "n_rows_total": len(panel),
        "n_dates_total": n_dates,
        "date_range": [str(unique_dates.min()), str(unique_dates.max())],
        "label_config": {"profit_take": 0.08, "stop_loss": 0.08, "max_holding": _LABEL_HORIZON},
        "purged_kfold_oos": _agg(fold_results["purged_kfold"]),
        "walk_forward_oos": _agg(fold_results["walk_forward"]),
        "purged_kfold_fold_detail": fold_results["purged_kfold"],
        "walk_forward_fold_detail": fold_results["walk_forward"],
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2, default=str)

    logger.info("训练完成，run_id=%s，样本外指标: %s", run_id, {
        "purged_kfold": metadata["purged_kfold_oos"],
        "walk_forward": metadata["walk_forward_oos"],
    })
    return metadata


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_training()
    print(json.dumps(
        {k: v for k, v in result.items() if not k.endswith("_detail")},
        ensure_ascii=False, indent=2, default=str,
    ))
