"""
持仓组合风险指标：集中度、历史波动率、相关性矩阵、历史法VaR/CVaR、回撤。

理论依据：`docs/04-风险管理/`、`docs/07-产品设计启示/02-场景化建议引擎.md`"建议卡片"
schema里的`risks[]`字段、`docs/07-产品设计启示/03-MVP路线图.md` Phase1条款"集中度/波动/
简化回撤风险"。

**范围说明（如实标注，对齐vibe coding八荣八耻第2条）**：本项目不接入券商持仓API
（MVP路线图明确"账户导入/手动持仓"），因此持仓数据来自用户在 Streamlit 界面手动录入的
`positions` 表（见 `common/db.py`），不是实时从券商同步的准确仓位。VaR/CVaR用历史模拟法
（历史收益率经验分布的分位数），不是参数法(方差-协方差)或蒙特卡洛法——数据量（几年日频）
和使用场景（个人研究工具，非机构风控）下历史模拟法足够且最容易解释给非专业用户，
这是一个明确的方法选择，不是能力不足的妥协。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("risk.portfolio_risk")

_LOOKBACK_DEFAULT = 250  # 约1年交易日，用于波动率/相关性/VaR估计


def load_open_positions(conn) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT lot_id, symbol, shares, cost_price, opened_at, note
        FROM positions
        WHERE is_closed = FALSE
        ORDER BY symbol, opened_at
        """
    ).df()


def load_price_history(conn, symbols: list[str], as_of_date: str, lookback: int) -> pd.DataFrame:
    """公开导出（非下划线私有），供 `risk/portfolio_optimizer.py` 复用同一段取价逻辑，
    不重复实现（对齐vibe coding八荣八耻第4条"复用存量"）。"""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "trade_date", "close"])
    placeholders = ",".join(["?"] * len(symbols))
    df = conn.execute(
        f"""
        SELECT symbol, trade_date, close
        FROM daily_quotes
        WHERE symbol IN ({placeholders}) AND adjust = 'qfq' AND trade_date <= ?
        ORDER BY symbol, trade_date
        """,
        symbols + [as_of_date],
    ).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    # 每只股票只保留最近 lookback 个交易日；用 GroupBy.tail()（不是 groupby().apply()）
    # 是因为本环境pandas版本的 apply(group_keys=False) 会把分组列(symbol)从结果里丢掉，
    # 是真实验证过的行为差异，不是猜测（2026-08-26 用 scripts/_tmp_debug_risk.py 实测发现）。
    return df.groupby("symbol", as_index=False).tail(lookback)


def compute_position_summary(conn, as_of_date: str) -> pd.DataFrame:
    """给每笔持仓算最新市值、浮盈亏；同symbol多笔lot会分别列出，聚合请在调用方用
    groupby('symbol')。"""
    positions = load_open_positions(conn)
    if positions.empty:
        return positions.assign(last_close=pd.Series(dtype=float), market_value=pd.Series(dtype=float),
                                 unrealized_pnl=pd.Series(dtype=float), unrealized_pnl_pct=pd.Series(dtype=float))
    symbols = positions["symbol"].unique().tolist()
    placeholders = ",".join(["?"] * len(symbols))
    latest = conn.execute(
        f"""
        SELECT symbol, close AS last_close
        FROM daily_quotes
        WHERE symbol IN ({placeholders}) AND adjust = 'qfq' AND trade_date <= ?
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) = 1
        """,
        symbols + [as_of_date],
    ).df()
    out = positions.merge(latest, on="symbol", how="left")
    out["market_value"] = out["shares"] * out["last_close"]
    out["cost_value"] = out["shares"] * out["cost_price"]
    out["unrealized_pnl"] = out["market_value"] - out["cost_value"]
    out["unrealized_pnl_pct"] = out["unrealized_pnl"] / out["cost_value"].replace(0, np.nan)
    return out


def compute_concentration(position_summary: pd.DataFrame) -> pd.DataFrame:
    """按symbol聚合后的持仓占净值比例（净值=全部持仓市值之和，不含现金——现金规模
    未纳入本系统建模范围，用户需自行心算现金比例）。"""
    if position_summary.empty:
        return pd.DataFrame(columns=["symbol", "market_value", "weight_pct"])
    agg = position_summary.groupby("symbol", as_index=False)["market_value"].sum()
    total = agg["market_value"].sum()
    agg["weight_pct"] = agg["market_value"] / total if total else np.nan
    return agg.sort_values("weight_pct", ascending=False)


def compute_volatility_and_var(
    conn,
    symbols: list[str],
    as_of_date: str,
    lookback: int = _LOOKBACK_DEFAULT,
    var_confidence: float = 0.95,
) -> pd.DataFrame:
    """逐股票年化波动率 + 历史模拟法单日VaR/CVaR（相对权重不涉及，这里是"单只股票
    满仓时"的分位数收益，组合层面的VaR见 compute_portfolio_var）。"""
    prices = load_price_history(conn, symbols, as_of_date, lookback + 1)
    rows = []
    for symbol, grp in prices.groupby("symbol"):
        ret = grp["close"].pct_change().dropna()
        if len(ret) < 20:
            rows.append({"symbol": symbol, "annual_vol": np.nan, "var_95_1d": np.nan, "cvar_95_1d": np.nan,
                         "n_obs": len(ret)})
            continue
        var_q = ret.quantile(1 - var_confidence)
        cvar = ret[ret <= var_q].mean()
        rows.append({
            "symbol": symbol,
            "annual_vol": round(float(ret.std() * np.sqrt(252)), 4),
            "var_95_1d": round(float(var_q), 4),
            "cvar_95_1d": round(float(cvar), 4) if pd.notna(cvar) else None,
            "n_obs": len(ret),
        })
    return pd.DataFrame(rows)


def compute_portfolio_correlation(conn, symbols: list[str], as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> pd.DataFrame:
    """持仓股票两两日收益相关系数矩阵——分散度不足的直接证据（都买同一批高相关股票，
    "分散持仓"只是名义上的，跟买一只没有本质区别）。"""
    prices = load_price_history(conn, symbols, as_of_date, lookback + 1)
    if prices.empty:
        return pd.DataFrame()
    wide = prices.pivot(index="trade_date", columns="symbol", values="close").pct_change().dropna(how="all")
    return wide.corr()


def compute_portfolio_var(
    conn,
    position_summary: pd.DataFrame,
    as_of_date: str,
    lookback: int = _LOOKBACK_DEFAULT,
    var_confidence: float = 0.95,
) -> dict:
    """组合层面历史模拟法VaR：把每只股票当天的真实历史收益按当前持仓权重加权求和，
    重放lookback天，再取经验分位数——直接考虑了个股间的真实历史相关性，不需要额外
    估计协方差矩阵。"""
    conc = compute_concentration(position_summary)
    if conc.empty:
        return {"status": "无持仓"}
    symbols = conc["symbol"].tolist()
    weights = conc.set_index("symbol")["weight_pct"]
    prices = load_price_history(conn, symbols, as_of_date, lookback + 1)
    if prices.empty:
        return {"status": "无价格历史"}
    wide = prices.pivot(index="trade_date", columns="symbol", values="close").pct_change().dropna(how="all")
    wide = wide.fillna(0.0)
    aligned_weights = weights.reindex(wide.columns).fillna(0.0)
    port_ret = wide.mul(aligned_weights, axis=1).sum(axis=1)
    if len(port_ret) < 20:
        return {"status": f"历史样本不足({len(port_ret)}天)，VaR估计不可靠"}
    var_q = port_ret.quantile(1 - var_confidence)
    cvar = port_ret[port_ret <= var_q].mean()
    return {
        "status": "ok",
        "n_obs": len(port_ret),
        "confidence": var_confidence,
        "var_1d_pct": round(float(var_q), 4),
        "cvar_1d_pct": round(float(cvar), 4) if pd.notna(cvar) else None,
        "annualized_vol": round(float(port_ret.std() * np.sqrt(252)), 4),
    }


def compute_max_drawdown_since(conn, position_summary: pd.DataFrame, as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> dict:
    """按当前持仓权重重放历史，算模拟净值曲线的最大回撤——是"如果一直按现在的权重
    持有"的假设性回撤，不是真实历史逐日调仓后的回撤（用户实际调过仓，真实回撤未知，
    这是明确的简化假设）。"""
    conc = compute_concentration(position_summary)
    if conc.empty:
        return {"status": "无持仓"}
    symbols = conc["symbol"].tolist()
    weights = conc.set_index("symbol")["weight_pct"]
    prices = load_price_history(conn, symbols, as_of_date, lookback + 1)
    if prices.empty:
        return {"status": "无价格历史"}
    wide = prices.pivot(index="trade_date", columns="symbol", values="close").pct_change().fillna(0.0)
    aligned_weights = weights.reindex(wide.columns).fillna(0.0)
    port_ret = wide.mul(aligned_weights, axis=1).sum(axis=1)
    cum = (1 + port_ret).cumprod()
    drawdown = cum / cum.cummax() - 1
    return {
        "status": "ok",
        "max_drawdown": round(float(drawdown.min()), 4),
        "current_drawdown": round(float(drawdown.iloc[-1]), 4),
        "n_obs": len(port_ret),
    }


def generate_risk_report(conn, as_of_date: str, lookback: int = _LOOKBACK_DEFAULT) -> dict:
    position_summary = compute_position_summary(conn, as_of_date)
    if position_summary.empty:
        return {"as_of": as_of_date, "status": "无持仓记录，风险报告为空"}
    symbols = position_summary["symbol"].unique().tolist()
    concentration = compute_concentration(position_summary)
    vol_var = compute_volatility_and_var(conn, symbols, as_of_date, lookback)
    corr = compute_portfolio_correlation(conn, symbols, as_of_date, lookback)
    port_var = compute_portfolio_var(conn, position_summary, as_of_date, lookback)
    drawdown = compute_max_drawdown_since(conn, position_summary, as_of_date, lookback)
    max_single_weight = float(concentration["weight_pct"].max()) if not concentration.empty else None
    return {
        "as_of": as_of_date,
        "status": "ok",
        "n_positions": len(concentration),
        "max_single_weight": round(max_single_weight, 4) if max_single_weight is not None else None,
        "concentration_warning": max_single_weight is not None and max_single_weight > 0.15,
        "concentration": concentration.to_dict(orient="records"),
        "volatility_var_by_symbol": vol_var.to_dict(orient="records"),
        "correlation_matrix": corr.round(3).to_dict() if not corr.empty else {},
        "portfolio_var": port_var,
        "drawdown_since_current_weights": drawdown,
    }
