"""五模型同一标准打分：不替换冠军。

标准对齐 Phase 1/3 与 `research/backtest.py`：
  - 沪深池、扩展窗口 Walk-Forward
  - RankIC = 预测分数与下个月收益的 Spearman
  - Top-30 等权持有到下月末，剔除涨停封死和停牌，扣往返费用
  - 头尾组 Q5−Q1 的月均收益和 t 检验

冠军不使用已经在全历史上训过的 `model.pkl` 回测（那会把测试月包含进训练）。
这里按冠军同一配方重跑 Walk-Forward：LambdaRank + 三重障碍标签
（`research/train_lightgbm.py` / `research/labeling.py`，波动率缩放保持默认）。

不写 champion.json / champion_hs.json / champion_bj.json。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from research.a_share_rules import CostAssumptions
from research.backtest import run_portfolio_backtest, summarize_performance
from research.factor_eval import FACTOR_COLS, add_forward_return
from research.train_lightgbm import attach_triple_barrier_labels, evaluate_fold
from scripts.run_challenger_bakeoff import (
    _champion_mtimes,
    _fit_rank_fold,
    _paired_net_vs_first,
    _walk_forward_frames,
)

logger = logging.getLogger("scripts.score_same_protocol")

_BAKEOFF_OOS = {
    "lgb_rank_quintile": "mlops/registry/bakeoff_20260922_135551/oos_lgb_rank_quintile.parquet",
    "lgb_rank_quintile_indus": "mlops/registry/bakeoff_20260922_135551/oos_lgb_rank_quintile_indus.parquet",
    "lgb_regress_fwd": "mlops/registry/bakeoff_20260922_135551/oos_lgb_regress_fwd.parquet",
    "ridge_fwd": "mlops/registry/bakeoff_20260922_135551/oos_ridge_fwd.parquet",
}
_BASE = "lgb_rank_barrier"
_DESC = {
    _BASE: "冠军同款配方：LambdaRank + 三重障碍标签。本轮按同一 Walk-Forward 重跑样本外。",
    "lgb_rank_quintile": "LambdaRank，下月收益五档",
    "lgb_rank_quintile_indus": "LambdaRank，下月收益五档，因子行业内去均值",
    "lgb_regress_fwd": "LightGBM 回归，直接预测下月收益",
    "ridge_fwd": "岭回归，直接预测下月收益",
}


def _load_hs_panel(panel_path: str) -> pd.DataFrame:
    panel = pd.read_parquet(panel_path)
    panel = panel[panel["exchange"].isin(["sh", "sz"])].copy()
    panel = add_forward_return(panel)
    panel = panel.dropna(subset=["fwd_return"])
    counts = panel.groupby("trade_date")["symbol"].transform("size")
    panel = panel[counts >= 30].copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.date
    return panel


def _train_barrier_oos(panel: pd.DataFrame) -> pd.DataFrame:
    logger.info("给面板打三重障碍标签（要读日线，可能要几分钟）")
    labeled = attach_triple_barrier_labels(panel)
    cols = [c for c in FACTOR_COLS if c in labeled.columns]
    pieces = []
    for fold_i, (train_df, test_df) in enumerate(_walk_forward_frames(labeled), start=1):
        trainable = train_df.dropna(subset=["label"]).copy()
        trainable["relevance"] = (trainable["label"].astype(int) + 1).astype(int)
        logger.info(
            "冠军配方第 %d 折 train=%d（有标签 %d） test=%d",
            fold_i,
            len(train_df),
            len(trainable),
            len(test_df),
        )
        pred = _fit_rank_fold(trainable, test_df, cols)
        pieces.append(pred)
    return pd.concat(pieces, ignore_index=True)


def _normalize_oos(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    keep = ["symbol", "trade_date", "pred_score", "fwd_return", "exchange"]
    return out[keep].drop_duplicates(["symbol", "trade_date"])


def _intersect(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    common = None
    for df in frames.values():
        keys = df[["symbol", "trade_date"]].drop_duplicates()
        common = keys if common is None else common.merge(keys, on=["symbol", "trade_date"])
    if common is None or common.empty:
        raise RuntimeError("五个模型没有共同的样本外股票-日期")
    logger.info("共同样本外行数 %d，截面日 %d", len(common), common["trade_date"].nunique())
    return {name: df.merge(common, on=["symbol", "trade_date"], how="inner") for name, df in frames.items()}


def _q5_minus_q1(oos: pd.DataFrame) -> dict:
    spreads = []
    for _, grp in oos.groupby("trade_date"):
        sub = grp.dropna(subset=["pred_score", "fwd_return"])
        if len(sub) < 50:
            continue
        bucket = pd.qcut(sub["pred_score"].rank(method="first"), 5, labels=False)
        spreads.append(float(sub.loc[bucket == 4, "fwd_return"].mean() - sub.loc[bucket == 0, "fwd_return"].mean()))
    arr = np.asarray(spreads, dtype=float)
    t_stat, p_value = stats.ttest_1samp(arr, 0.0)
    return {
        "n_months": int(len(arr)),
        "mean_monthly": round(float(arr.mean()), 4),
        "t_stat": round(float(t_stat), 3),
        "p_value": round(float(p_value), 4),
    }


def _score(oos: pd.DataFrame) -> tuple[dict, pd.Series]:
    rank = evaluate_fold(oos)
    bt = run_portfolio_backtest(oos, top_n=30, cost_assumptions=CostAssumptions(), use_tradability=True)
    perf = summarize_performance(bt)
    bench = oos.groupby("trade_date")["fwd_return"].mean().rename("bench_return")
    joined = bt.join(bench)
    excess = joined["net_return"] - joined["bench_return"]
    perf["benchmark_equal_weight"] = {
        "excess_return_mean_monthly": round(float(excess.mean()), 4),
        "excess_return_win_rate": round(float((excess > 0).mean()), 3),
    }
    perf["rank_ic"] = rank
    perf["q5_minus_q1_gross"] = _q5_minus_q1(oos)
    perf["tradability_filter"] = True
    return perf, bt["net_return"]


def _verdict(scores: dict, paired: dict) -> str:
    base_ic = scores[_BASE]["rank_ic"]["rank_ic_mean"]
    base_cagr = scores[_BASE]["net"]["cagr"]
    lines = [
        f"冠军同款配方的样本外 RankIC 是 {base_ic}，扣费且去掉涨停/停牌后的 Top-30 年化是 {base_cagr:.1%}。",
    ]
    beaten = []
    for name, payload in paired.items():
        p_value = payload.get("p_value")
        diff = payload.get("mean_net_diff_monthly")
        ic = scores[name]["rank_ic"]["rank_ic_mean"]
        if p_value is None:
            continue
        if p_value < 0.05 and diff is not None and diff > 0 and ic > base_ic:
            beaten.append(name)
    if not beaten:
        lines.append(
            "没有任何挑战者在「每月买前 30 名」的扣费收益上显著超过这套冠军配方（配对 p≥0.05），"
            "或者 RankIC 并没有同时更高。因此不替换现行冠军。"
        )
    else:
        lines.append(
            "按配对检验，下列模型 RankIC 更高且 Top-30 月收益显著更高："
            + "、".join(beaten)
            + "。本次仍不改冠军文件，只记录这个结果。"
        )
    return "".join(lines)


def run(panel_path: str = "data/feature_panel.parquet", registry_dir: str = "mlops/registry") -> dict:
    registry = Path(registry_dir)
    mtimes_before = _champion_mtimes(registry)
    panel = _load_hs_panel(panel_path)
    barrier = _normalize_oos(_train_barrier_oos(panel))

    frames = {_BASE: barrier}
    for name, path in _BAKEOFF_OOS.items():
        frames[name] = _normalize_oos(pd.read_parquet(path))
    frames = _intersect(frames)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = registry / f"same_protocol_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames[_BASE].to_parquet(out_dir / "oos_lgb_rank_barrier.parquet", index=False)

    scores = {}
    nets = {}
    for name, df in frames.items():
        logger.info("打分 %s 行数=%d", name, len(df))
        scores[name], nets[name] = _score(df)

    ordered_nets = {_BASE: nets[_BASE], **{k: nets[k] for k in _BAKEOFF_OOS}}
    paired = _paired_net_vs_first(ordered_nets)
    summary = {
        "scored_at": dt.datetime.now().isoformat(),
        "pool": "hs",
        "champion_replaced": False,
        "protocol": {
            "split": "walk_forward_expanding",
            "common_rows": int(len(frames[_BASE])),
            "common_dates": int(frames[_BASE]["trade_date"].nunique()),
            "rank_metric": "monthly Spearman(pred_score, fwd_return)",
            "portfolio": "top_30_equal_weight_next_month",
            "tradability_filter": True,
            "costs": "round_trip commission+stamp+transfer + slippage_rate once",
            "note": "五个模型先取股票-日期交集，再在同一批样本上算 RankIC 和 Top-30。",
        },
        "published_champion_reference": {
            "run_id": "20260918_232953",
            "walk_forward_rank_ic_mean": 0.0554,
            "note": "这是上次训练登记的折均值。本次 lgb_rank_barrier 是同一配方的重新样本外，月份对齐以本次为准。",
        },
        "variants": {
            name: {"description": _DESC[name], "scores": scores[name]} for name in ordered_nets
        },
        "paired_top30_net_vs_barrier": paired,
        "verdict": _verdict(scores, paired),
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    if _champion_mtimes(registry) != mtimes_before:
        raise RuntimeError("冠军指针被改动了")
    logger.info("完成 %s", out_dir)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run()
    brief = {
        name: {
            "rank_ic": payload["scores"]["rank_ic"]["rank_ic_mean"],
            "rank_ic_positive": payload["scores"]["rank_ic"]["pct_positive"],
            "top30_net_cagr": payload["scores"]["net"]["cagr"],
            "top30_net_sharpe": payload["scores"]["net"]["sharpe"],
            "top30_max_drawdown": payload["scores"]["net"]["max_drawdown"],
            "excess_monthly": payload["scores"]["benchmark_equal_weight"]["excess_return_mean_monthly"],
            "q5_q1_monthly": payload["scores"]["q5_minus_q1_gross"]["mean_monthly"],
            "q5_q1_p": payload["scores"]["q5_minus_q1_gross"]["p_value"],
        }
        for name, payload in result["variants"].items()
    }
    print(json.dumps({"verdict": result["verdict"], "brief": brief, "paired": result["paired_top30_net_vs_barrier"]}, ensure_ascii=False, indent=2))
