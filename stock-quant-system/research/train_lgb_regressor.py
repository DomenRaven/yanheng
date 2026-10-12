"""沪深回归树的正式模型包。不替换冠军。

规格对齐 `research/train_lightgbm.py` 的生产产物：
  - 同一张沪深月末因子面板、同一套扩展窗口 Walk-Forward
  - 样本外指标写入 metadata（RankIC 对下月收益）
  - 再用全部已有下月收益的样本拟合最终 `model.pkl`，供当天快照打分
  - 登记进 `model_catalog.json`，由界面切换加载

目标与冠军不同：这里直接回归下月收益，不是三重障碍的 LambdaRank。
超参与 `scripts/run_challenger_bakeoff.py` 的 `lgb_regress_fwd` 相同。
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
from research.train_lightgbm import _date_level_splits, evaluate_fold

logger = logging.getLogger("research.train_lgb_regressor")

_PARAMS = {
    "objective": "regression",
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "random_state": 42,
    "verbosity": -1,
    "n_jobs": 4,
}
_CHAMPION_FILES = ("champion.json", "champion_hs.json", "champion_bj.json")


def _clip_features(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    frame = df[cols].copy()
    for col in cols:
        series = frame[col]
        if series.notna().sum() < 10:
            continue
        lo, hi = series.quantile(0.01), series.quantile(0.99)
        frame[col] = series.clip(lo, hi)
    return frame


def _load_panel(panel_path: str) -> pd.DataFrame:
    panel = pd.read_parquet(panel_path)
    panel = panel[panel["exchange"].isin(["sh", "sz"])].copy()
    panel = add_forward_return(panel)
    panel = panel.dropna(subset=["fwd_return"])
    counts = panel.groupby("trade_date")["symbol"].transform("size")
    panel = panel[counts >= 30].copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.date
    return panel


def _walk_forward(panel: pd.DataFrame, cols: list[str]) -> tuple[list[dict], pd.DataFrame]:
    unique_dates = np.array(sorted(panel["trade_date"].unique()))
    n_dates = len(unique_dates)
    train_size = max(24, n_dates // 3)
    test_size = max(6, n_dates // 10)
    folds = []
    pieces = []
    for train_dates, test_dates in _date_level_splits(
        unique_dates, "walk_forward", train_size=train_size, test_size=test_size, expanding=True
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)].sort_values("trade_date")
        test_df = panel[panel["trade_date"].isin(test_dates)].sort_values("trade_date")
        if train_df.empty or test_df.empty:
            continue
        model = lgb.LGBMRegressor(**_PARAMS)
        model.fit(_clip_features(train_df, cols), train_df["fwd_return"].astype(float))
        pred = test_df.copy()
        pred["pred_score"] = model.predict(_clip_features(test_df, cols))
        folds.append(evaluate_fold(pred))
        pieces.append(pred)
    oos = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    return folds, oos


def _agg(folds: list[dict]) -> dict:
    valid = [f for f in folds if f.get("n_periods", 0) > 0]
    if not valid:
        return {"n_folds": 0}
    return {
        "n_folds": len(valid),
        "rank_ic_mean": round(float(np.mean([f["rank_ic_mean"] for f in valid])), 4),
        "pct_positive_mean": round(float(np.mean([f["pct_positive"] for f in valid])), 3),
    }


def run_training(
    panel_path: str = "data/feature_panel.parquet",
    registry_dir: str = "mlops/registry",
) -> dict:
    registry = Path(registry_dir)
    mtimes = {
        name: (registry / name).stat().st_mtime if (registry / name).exists() else None
        for name in _CHAMPION_FILES
    }
    panel = _load_panel(panel_path)
    cols = [c for c in FACTOR_COLS if c in panel.columns]
    folds, _oos = _walk_forward(panel, cols)

    panel_sorted = panel.sort_values("trade_date")
    model = lgb.LGBMRegressor(**_PARAMS)
    model.fit(_clip_features(panel_sorted, cols), panel_sorted["fwd_return"].astype(float))

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_reg"
    run_dir = registry / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "model.pkl", "wb") as f:
        pickle.dump(model, f)

    unique_dates = sorted(panel["trade_date"].unique())
    metadata = {
        "run_id": run_id,
        "trained_at": dt.datetime.now().isoformat(),
        "pool_id": "hs",
        "model_family": "lgb_regressor",
        "label": "回归树",
        "replaces_champion": False,
        "feature_cols": cols,
        "n_rows_total": int(len(panel)),
        "n_dates_total": int(panel["trade_date"].nunique()),
        "date_range": [str(unique_dates[0]), str(unique_dates[-1])],
        "label_config": {
            "target": "fwd_return",
            "objective": "regression",
            "note": "下个月末收益。不是三重障碍，也不改建议卡片的 ±8% 止盈止损。",
        },
        "hyperparams": {k: v for k, v in _PARAMS.items() if k != "verbosity"},
        "walk_forward_oos": _agg(folds),
        "walk_forward_fold_detail": folds,
        "final_fit": "all_rows_with_forward_return",
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    from advice.model_selection import register_selectable

    register_selectable(
        registry,
        run_id=run_id,
        label="回归树",
        pool_id="hs",
        objective="regression",
    )
    after = {
        name: (registry / name).stat().st_mtime if (registry / name).exists() else None
        for name in _CHAMPION_FILES
    }
    if after != mtimes:
        raise RuntimeError("正式化回归树时改动了冠军指针")
    logger.info("回归树已登记 run_id=%s，冠军指针未改", run_id)
    return metadata


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_training()
    print(json.dumps(
        {"run_id": result["run_id"], "walk_forward_oos": result["walk_forward_oos"]},
        ensure_ascii=False,
        indent=2,
    ))
