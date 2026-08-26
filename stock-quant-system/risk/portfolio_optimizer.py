"""
组合优化器：均值-方差（Alpha − λ·风险）与风险平价两种目标，把"模型打分 + 持仓状态"
转成"目标权重"，再与当前权重比较给出再平衡建议。

理论依据：`docs/03-量化方法/04-组合优化.md` 第1节"最大化 Alpha − λ·风险"/"风险平价"，
第4节"协方差收缩(Ledoit-Wolf)"数值技巧；`docs/00-总览/02-产品定位与边界.md` Should(V1)
条款"组合优化（约束均值方差 / 风险平价简化版）"。

**如实标注（对齐vibe coding八荣八耻第2条"对齐需求"）**：这是Phase4验收时对照产品边界
文档发现的一个Should级缺口——原Phase2-4任务列表里没有单独列出"组合优化"这一项，是
补齐MVP路线图里明确写的Should条款，不是最初计划的一部分，这里如实记录，不假装它一直
在计划内。

**预期收益估计方法（Grinold-Kahn"主动管理基本法则"精化Alpha公式，Active Portfolio
Management, Grinold & Kahn）**：
    alpha_i = assumed_IC × sigma_i × score_z_i
其中 score_z_i 是模型打分在候选池内的横截面z-score，sigma_i 是个股历史年化波动率，
assumed_IC 是"假设的信息系数"（保守取0.03——低于`docs/phase1-acceptance-report.md`
里实测样本外RankIC，因为组合优化对alpha估计误差敏感，保守收缩可以避免优化器对着
噪音下重注，这与协方差收缩是同一个"不完全相信估计值"的思路）。这不是臆造公式，是
量化行业构建组合时把"排序分数"转成"可优化的收益预期"的标准写法。

**协方差估计**：用 `sklearn.covariance.LedoitWolf` 收缩估计而不是原始样本协方差矩阵
——候选数（约30~80）接近甚至超过样本天数(250天)量级时，原始样本协方差数值不稳定，
Ledoit-Wolf收缩是`docs/03-量化方法/04-组合优化.md`第4节明确建议的工业技巧。
scikit-learn是项目已有依赖（`requirements.txt`），不新增包（复用存量原则）。

**范围说明（如实标注局限）**：
- 这是"个人研究工具"级别的简化优化器，不是机构级多因子风险模型(Barra)驱动的优化——
  没有行业/风格中性化约束，只有单票集中度上限；`docs/03-量化方法/04-组合优化.md`
  第2节里的其余约束（行业偏离、换手率上限等）本版本未实现，留作后续扩展，不是"做完了"。
- 输出是"目标权重建议"，不自动下单——严格对齐`docs/00-总览/02-产品定位与边界.md`的
  Won't清单，用户需要自行在券商App操作。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf

from risk.portfolio_risk import load_price_history

logger = logging.getLogger("risk.portfolio_optimizer")

_DEFAULT_LOOKBACK = 250
_DEFAULT_ASSUMED_IC = 0.03
_MIN_OBS_FOR_COV = 60  # 少于60个交易日的协方差估计太不稳定，直接跳过优化


def _prepare_returns_matrix(conn, symbols: list[str], as_of_date: str, lookback: int) -> pd.DataFrame:
    prices = load_price_history(conn, symbols, as_of_date, lookback + 1)
    if prices.empty:
        return pd.DataFrame()
    return prices.pivot(index="trade_date", columns="symbol", values="close").pct_change().dropna(how="all")


def _estimate_alpha(pred_scores: pd.Series, ann_vol: pd.Series, assumed_ic: float) -> pd.Series:
    """Grinold-Kahn精化Alpha：alpha = IC × sigma × score_z。"""
    std = pred_scores.std(ddof=0)
    z = (pred_scores - pred_scores.mean()) / (std if std > 1e-8 else 1.0)
    return assumed_ic * ann_vol.reindex(z.index).fillna(ann_vol.median()) * z


def _mean_variance_weights(alpha: np.ndarray, cov: np.ndarray, single_name_cap: float, risk_aversion: float) -> np.ndarray:
    n = len(alpha)
    x0 = np.full(n, 1.0 / n)

    def objective(w):
        return -(w @ alpha - 0.5 * risk_aversion * (w @ cov @ w))

    constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
    bounds = [(0.0, single_name_cap)] * n
    res = minimize(objective, x0, method="SLSQP", bounds=bounds, constraints=constraints,
                   options={"maxiter": 500, "ftol": 1e-10})
    if not res.success:
        logger.warning("均值-方差优化未收敛(%s)，退化为等权重", res.message)
        return x0
    w = np.clip(res.x, 0.0, None)
    total = w.sum()
    return w / total if total > 0 else x0


def _risk_parity_weights(cov: np.ndarray, single_name_cap: float) -> np.ndarray:
    n = cov.shape[0]
    x0 = np.full(n, 1.0 / n)

    def objective(w):
        port_var = w @ cov @ w
        marginal = cov @ w
        risk_contrib = w * marginal
        target = port_var / n
        return float(np.sum((risk_contrib - target) ** 2))

    constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
    bounds = [(1e-6, single_name_cap)] * n
    res = minimize(objective, x0, method="SLSQP", bounds=bounds, constraints=constraints,
                   options={"maxiter": 500, "ftol": 1e-12})
    if not res.success:
        logger.warning("风险平价优化未收敛(%s)，退化为等权重", res.message)
        return x0
    w = np.clip(res.x, 0.0, None)
    total = w.sum()
    return w / total if total > 0 else x0


def optimize_target_weights(
    conn,
    candidate_symbols: list[str],
    pred_scores: dict[str, float],
    as_of_date: str,
    method: str = "mean_variance",
    single_name_cap: float = 0.15,
    lookback: int = _DEFAULT_LOOKBACK,
    risk_aversion: float = 3.0,
    assumed_ic: float = _DEFAULT_ASSUMED_IC,
) -> pd.DataFrame:
    """输出 candidate_symbols 里每只股票的目标权重。数据不足或优化失败时返回空表
    （调用方应把"无法优化"当成"维持现状"处理，不是报错中断整条建议生成流程）。"""
    candidate_symbols = list(dict.fromkeys(candidate_symbols))  # 去重保序
    if len(candidate_symbols) < 2:
        return pd.DataFrame(columns=["symbol", "target_weight"])

    returns = _prepare_returns_matrix(conn, candidate_symbols, as_of_date, lookback)
    if returns.empty:
        return pd.DataFrame(columns=["symbol", "target_weight"])
    usable = [s for s in candidate_symbols if s in returns.columns and returns[s].notna().sum() >= _MIN_OBS_FOR_COV]
    if len(usable) < 2:
        logger.info("可用历史数据的候选不足2只(%d)，跳过组合优化", len(usable))
        return pd.DataFrame(columns=["symbol", "target_weight"])

    ret_mat = returns[usable].fillna(0.0)
    cov_annual = LedoitWolf().fit(ret_mat.values).covariance_ * 252
    ann_vol = pd.Series(np.sqrt(np.diag(cov_annual)), index=usable)

    if method == "risk_parity":
        w = _risk_parity_weights(cov_annual, single_name_cap)
    else:
        scores = pd.Series({s: pred_scores.get(s, np.nan) for s in usable}).astype(float)
        scores = scores.fillna(scores.median())
        alpha = _estimate_alpha(scores, ann_vol, assumed_ic).reindex(usable).values
        w = _mean_variance_weights(alpha, cov_annual, single_name_cap, risk_aversion)

    return pd.DataFrame({"symbol": usable, "target_weight": w})


def suggest_rebalance(
    conn,
    current_weights: pd.DataFrame,
    candidate_symbols: list[str],
    pred_scores: dict[str, float],
    as_of_date: str,
    method: str = "mean_variance",
    drift_threshold: float = 0.03,
    **kwargs,
) -> pd.DataFrame:
    """给定当前持仓权重(`current_weights` 列: symbol, weight_pct，来自
    `risk/portfolio_risk.py::compute_concentration()`) + 候选池(通常=当前持仓∪掘金Top
    候选)，输出优化目标权重与当前权重的差异表；|diff| < drift_threshold 的不建议调整
    （避免为数值噪音瞎折腾换手成本，对齐`docs/03-量化方法/04-组合优化.md`第4节
    "正则化与换手惩罚"的产品化简化版）。"""
    target = optimize_target_weights(conn, candidate_symbols, pred_scores, as_of_date, method=method, **kwargs)
    if target.empty:
        return pd.DataFrame(columns=["symbol", "current_weight", "target_weight", "diff", "action_hint"])

    cur = current_weights.set_index("symbol")["weight_pct"] if not current_weights.empty else pd.Series(dtype=float)
    out = target.set_index("symbol")
    out["current_weight"] = cur.reindex(out.index).fillna(0.0)
    out["diff"] = out["target_weight"] - out["current_weight"]
    out["action_hint"] = np.where(
        out["diff"] > drift_threshold, "increase",
        np.where(out["diff"] < -drift_threshold, "decrease", "keep"),
    )
    return out.reset_index()[["symbol", "current_weight", "target_weight", "diff", "action_hint"]].sort_values(
        "diff", ascending=False
    )


if __name__ == "__main__":
    import datetime as dt

    from advice.scanner import run_scan
    from common.db import get_connection, init_schema
    from risk.portfolio_risk import compute_concentration, compute_position_summary

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    full_ranked, top_pick, trade_date = run_scan(top_n=30)
    conn = get_connection()
    init_schema(conn)
    try:
        position_summary = compute_position_summary(conn, str(trade_date))
        concentration = compute_concentration(position_summary)
        held = concentration["symbol"].tolist() if not concentration.empty else []
        candidates = list(dict.fromkeys(held + top_pick["symbol"].tolist()))
        pred_scores = dict(zip(full_ranked["symbol"], full_ranked["pred_score"]))
        result = suggest_rebalance(conn, concentration, candidates, pred_scores, str(trade_date))
    finally:
        conn.close()
    print(result.to_string(index=False) if not result.empty else "数据不足，无法给出组合优化建议")
