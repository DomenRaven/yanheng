"""挑战者同场打分：同一 Walk-Forward，不替换冠军。

对照：用户要求多训几个模型、用同一标准打分，冠军指针保持不动。
标准对齐 `research/train_lightgbm.py` 的扩展窗口 Walk-Forward，以及
`research/backtest.py` 的每月末 Top-30 等权、往返成本（本脚本默认
`use_tradability=False`，因为打分时不能和正在写库的数据更新抢 DuckDB）。

四个挑战者（都只在沪深池、同一批有下期收益的月末截面上比较）：
  1. lgb_rank_quintile — LambdaRank，相关度=截面收益五档
  2. lgb_rank_quintile_indus — 同上，因子先做行业内去均值
  3. lgb_regress_fwd — LightGBM 回归，直接拟合下期收益
  4. ridge_fwd — 岭回归拟合下期收益

标签依据：`docs/03-量化方法/10-时序验证与标签工程.md` 第6节，选股更贴近
学习排序；回归是对照，不是新原理。行业内去均值是 Barra 行业中性的简化，
不是完整风格中性。

产出：`mlops/registry/bakeoff_<stamp>/summary.json` 与各模型 OOS 分数。
不写 `champion.json` / `champion_hs.json` / `champion_bj.json`。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from research.a_share_rules import CostAssumptions
from research.backtest import run_portfolio_backtest, summarize_performance
from research.factor_eval import FACTOR_COLS, add_forward_return
from research.train_lightgbm import _date_level_splits, evaluate_fold

logger = logging.getLogger("scripts.run_challenger_bakeoff")

_CHAMPION_FILES = ("champion.json", "champion_hs.json", "champion_bj.json")
_LGB_RANK = {
    "objective": "lambdarank",
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "random_state": 42,
    "verbosity": -1,
    "n_jobs": 4,
}
_LGB_REGRESS = {
    "objective": "regression",
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "random_state": 42,
    "verbosity": -1,
    "n_jobs": 4,
}


def _champion_mtimes(registry: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name in _CHAMPION_FILES:
        path = registry / name
        out[name] = path.stat().st_mtime if path.exists() else None
    return out


def _quintile_relevance(panel: pd.DataFrame) -> pd.Series:
    """每个月末按下期收益分五档，0=最差，4=最好。只使用当日截面，不跨期。"""

    def _one(s: pd.Series) -> pd.Series:
        r = s.rank(method="average", pct=True)
        return np.floor(r * 5).clip(0, 4)

    return panel.groupby("trade_date", group_keys=False)["fwd_return"].transform(_one)


def _industry_demean(panel: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = panel.copy()
    grouped = out.groupby(["trade_date", "industry_code"], dropna=False)
    for col in cols:
        out[col] = out[col] - grouped[col].transform("mean")
    return out


def _clip_features(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    x = df[cols].copy()
    for col in cols:
        series = x[col]
        if series.notna().sum() < 10:
            continue
        lo, hi = series.quantile(0.01), series.quantile(0.99)
        x[col] = series.clip(lo, hi)
    return x


def _walk_forward_frames(panel: pd.DataFrame):
    unique_dates = np.array(sorted(panel["trade_date"].unique()))
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
        yield train_df, test_df


def _fit_rank_fold(train_df: pd.DataFrame, test_df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    train_df = train_df.sort_values("trade_date")
    test_df = test_df.sort_values("trade_date")
    x_train = _clip_features(train_df, cols)
    x_test = _clip_features(test_df, cols)
    y_train = train_df["relevance"].astype(int)
    group = train_df.groupby("trade_date", sort=True).size().to_numpy()
    model = lgb.LGBMRanker(**_LGB_RANK)
    model.fit(x_train, y_train, group=group)
    out = test_df.copy()
    out["pred_score"] = model.predict(x_test)
    return out


def _fit_regress_fold(train_df: pd.DataFrame, test_df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    train_df = train_df.sort_values("trade_date")
    test_df = test_df.sort_values("trade_date")
    x_train = _clip_features(train_df, cols)
    x_test = _clip_features(test_df, cols)
    model = lgb.LGBMRegressor(**_LGB_REGRESS)
    model.fit(x_train, train_df["fwd_return"].astype(float))
    out = test_df.copy()
    out["pred_score"] = model.predict(x_test)
    return out


def _fit_ridge_fold(train_df: pd.DataFrame, test_df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0))
    pipe.fit(train_df[cols], train_df["fwd_return"].astype(float))
    out = test_df.copy()
    out["pred_score"] = pipe.predict(test_df[cols])
    return out


def _score_oos(oos: pd.DataFrame) -> dict:
    rank = evaluate_fold(oos)
    bt = run_portfolio_backtest(oos, top_n=30, cost_assumptions=CostAssumptions(), use_tradability=False)
    perf = summarize_performance(bt)
    bench = oos.groupby("trade_date")["fwd_return"].mean().rename("bench_return")
    joined = bt.join(bench)
    excess = joined["net_return"] - joined["bench_return"]
    perf["benchmark_equal_weight"] = {
        "excess_return_mean_monthly": round(float(excess.mean()), 4),
        "excess_return_win_rate": round(float((excess > 0).mean()), 3),
    }
    perf["rank_ic"] = rank
    perf["tradability_filter"] = False
    return perf


def _paired_net_vs_first(returns_by_name: dict[str, pd.Series]) -> dict:
    names = list(returns_by_name)
    if len(names) < 2:
        return {}
    base = names[0]
    out = {}
    for name in names[1:]:
        paired = pd.concat([returns_by_name[base], returns_by_name[name]], axis=1, join="inner").dropna()
        if len(paired) < 3:
            out[name] = {"n": int(len(paired))}
            continue
        diff = paired.iloc[:, 1] - paired.iloc[:, 0]
        t_stat, p_value = stats.ttest_rel(paired.iloc[:, 1], paired.iloc[:, 0])
        out[name] = {
            "versus": base,
            "n_months": int(len(paired)),
            "mean_net_diff_monthly": round(float(diff.mean()), 4),
            "t_stat": round(float(t_stat), 3),
            "p_value": round(float(p_value), 4),
        }
    return out


def run_bakeoff(panel_path: str = "data/feature_panel.parquet", registry_dir: str = "mlops/registry") -> dict:
    registry = Path(registry_dir)
    mtimes_before = _champion_mtimes(registry)

    panel = pd.read_parquet(panel_path)
    panel = panel[panel["exchange"].isin(["sh", "sz"])].copy()
    panel = add_forward_return(panel)
    panel = panel.dropna(subset=["fwd_return"])
    counts = panel.groupby("trade_date")["symbol"].transform("size")
    panel = panel[counts >= 30].copy()
    panel["relevance"] = _quintile_relevance(panel).astype(int)

    cols = [c for c in FACTOR_COLS if c in panel.columns]
    indus = _industry_demean(panel, cols)

    variants = {
        "lgb_rank_quintile": (
            "LambdaRank，截面收益五档相关度，原始因子",
            panel,
            _fit_rank_fold,
        ),
        "lgb_rank_quintile_indus": (
            "LambdaRank，截面收益五档；因子在申万行业内去均值",
            indus,
            _fit_rank_fold,
        ),
        "lgb_regress_fwd": (
            "LightGBM 回归，直接预测下期收益",
            panel,
            _fit_regress_fold,
        ),
        "ridge_fwd": (
            "岭回归（中位数填补+标准化），直接预测下期收益",
            panel,
            _fit_ridge_fold,
        ),
    }

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = registry / f"bakeoff_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    scores: dict[str, dict] = {}
    net_series: dict[str, pd.Series] = {}
    descriptions: dict[str, str] = {}

    for name, (desc, frame, fitter) in variants.items():
        logger.info("开始训练 %s", name)
        descriptions[name] = desc
        pieces = []
        for fold_i, (train_df, test_df) in enumerate(_walk_forward_frames(frame), start=1):
            logger.info("%s 第 %d 折 train=%d test=%d", name, fold_i, len(train_df), len(test_df))
            pred = fitter(train_df, test_df, cols)
            pieces.append(pred)
        oos = pd.concat(pieces, ignore_index=True)
        keep = ["symbol", "trade_date", "pred_score", "fwd_return", "exchange"]
        oos[keep].to_parquet(out_dir / f"oos_{name}.parquet", index=False)
        scored = _score_oos(oos)
        scores[name] = scored
        bt = run_portfolio_backtest(oos, top_n=30, use_tradability=False)
        net_series[name] = bt["net_return"]
        logger.info(
            "%s RankIC=%s Top30净收益月均见 summary",
            name,
            scored["rank_ic"].get("rank_ic_mean"),
        )

    summary = {
        "bakeoff_id": stamp,
        "trained_at": dt.datetime.now().isoformat(),
        "pool": "hs",
        "panel_path": panel_path,
        "n_rows": int(len(panel)),
        "n_dates": int(panel["trade_date"].nunique()),
        "date_range": [str(panel["trade_date"].min()), str(panel["trade_date"].max())],
        "protocol": {
            "split": "walk_forward_expanding",
            "rank_metric": "monthly Spearman(pred_score, fwd_return)",
            "portfolio": "top_30_equal_weight_hold_to_next_month_end",
            "costs": "round_trip commission+stamp+transfer + slippage_rate once",
            "tradability_filter": False,
            "tradability_note": "未读 limit_price/停牌表。数据更新占用 DuckDB 时不能开第二连接。",
            "champion_replaced": False,
        },
        "reference_champion": {
            "run_id": "20260918_232953",
            "pool": "hs",
            "note": "现行冠军未参与本轮重训，也未替换。其登记的 Walk-Forward RankIC 均值是 0.0554，标签是三重障碍而不是本轮的收益五档，不能和下面的 RankIC 直接当成同一标签下的配对检验。",
        },
        "variants": {
            name: {"description": descriptions[name], "scores": scores[name]} for name in variants
        },
        "paired_top30_net_vs_first": _paired_net_vs_first(net_series),
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    mtimes_after = _champion_mtimes(registry)
    if mtimes_after != mtimes_before:
        raise RuntimeError(f"冠军指针被改动了: before={mtimes_before} after={mtimes_after}")
    logger.info("打分完成，冠军指针未变: %s", out_dir)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_bakeoff()
    brief = {
        name: {
            "rank_ic_mean": payload["scores"]["rank_ic"].get("rank_ic_mean"),
            "top30_net_cagr": (payload["scores"].get("net") or {}).get("cagr"),
            "top30_net_sharpe": (payload["scores"].get("net") or {}).get("sharpe"),
            "excess_monthly": payload["scores"]["benchmark_equal_weight"]["excess_return_mean_monthly"],
        }
        for name, payload in result["variants"].items()
    }
    print(json.dumps({"bakeoff_id": result["bakeoff_id"], "brief": brief}, ensure_ascii=False, indent=2))
