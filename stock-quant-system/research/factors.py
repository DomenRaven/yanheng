"""量价类因子：只依赖日线 OHLCV，不读财务表，也不打开主库。

输入约定：单标的按日期升序的 DataFrame，至少含 close，建议含 high/low/volume/turnover。
横截面中性化（行业/市值）不在本模块，留给接上行业分类表之后的面板层。

因子定义对齐理论库 `docs/05-工具库/03-指标与因子目录.md`。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _returns(close: pd.Series) -> pd.Series:
    return close.pct_change()


def mom_12_1(close: pd.Series, long_window: int = 252, skip: int = 21) -> pd.Series:
    """中期动量：过去约 12 个月收益，跳过最近 1 个月（避免短期反转污染）。"""
    if long_window <= skip:
        raise ValueError("long_window 必须大于 skip")
    return close.shift(skip) / close.shift(long_window) - 1


def rev_1m(close: pd.Series, window: int = 21) -> pd.Series:
    """短期反转：最近约 1 个月收益，预期与下期收益负相关（需实证检验）。"""
    return close / close.shift(window) - 1


def volatility(close: pd.Series, window: int = 20) -> pd.Series:
    return _returns(close).rolling(window).std()


def turnover_mean(turnover: pd.Series, window: int = 20) -> pd.Series:
    return turnover.rolling(window).mean()


def amplitude_mean(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> pd.Series:
    amp = (high - low) / close.replace(0, np.nan)
    return amp.rolling(window).mean()


def compute_price_factors(df: pd.DataFrame) -> pd.DataFrame:
    """给单标的行情表追加量价因子列，原列保留。"""
    out = df.copy()
    close = out["close"]
    out["factor_mom_12_1"] = mom_12_1(close)
    out["factor_rev_1m"] = rev_1m(close)
    out["factor_vol_20"] = volatility(close, 20)
    if "turnover" in out.columns:
        out["factor_turnover_20"] = turnover_mean(out["turnover"], 20)
    if {"high", "low"}.issubset(out.columns):
        out["factor_amp_20"] = amplitude_mean(out["high"], out["low"], close, 20)
    return out


# ============================================================
# 2026-08-26 Phase 1：价值/质量因子。
#
# 输入均为 Tushare daily_basic（估值比率，官方每日计算，天然 point-in-time）
# 或 fundamentals EAV 表 pivot 后的比率指标列（需要在 research/panel.py 里先按
# disclosure_calendar 做 point-in-time 对齐，本模块不关心对齐过程，只做"给定
# 已对齐好的比率值，如何转成因子"的纯函数计算，以保持与量价因子一致的"不摸库"
# 设计原则，方便离线单测）。
#
# 因子方向约定：所有 factor_* 列都遵循"数值越大越好/越值得买入"的统一符号，
# 与因子评估层（factor_eval.py）算 IC 时默认"因子与下期收益正相关"的假设对齐。
# ============================================================


def value_bp(pb: pd.Series) -> pd.Series:
    """账面市值比 BP = 1/PB。PB<=0（净资产为负，资不抵债）时无经济意义，记为NaN。"""
    pb = pb.where(pb > 0)
    return 1.0 / pb


def value_ep(pe_ttm: pd.Series) -> pd.Series:
    """盈利收益率 EP = 1/PE_TTM。PE_TTM<=0（亏损股）时不能直接取倒数变正号，记为NaN，
    避免"亏得越多、EP越大"这种反直觉排序污染因子。"""
    pe_ttm = pe_ttm.where(pe_ttm > 0)
    return 1.0 / pe_ttm


def value_sp(ps_ttm: pd.Series) -> pd.Series:
    """销售收益率 SP = 1/PS_TTM，营收基本不会为负，PS_TTM<=0视为数据异常。"""
    ps_ttm = ps_ttm.where(ps_ttm > 0)
    return 1.0 / ps_ttm


def size_log_mktcap(total_mv: pd.Series) -> pd.Series:
    """对数市值，规模因子标准做法；同时用于回归/排序时的市值中性化控制变量。"""
    return np.log(total_mv.where(total_mv > 0))


def quality_roe(roe: pd.Series) -> pd.Series:
    """净资产收益率，直接使用（新浪/Tushare口径均为百分数），数值越大越好。"""
    return roe


def quality_gross_margin(grossprofit_margin: pd.Series) -> pd.Series:
    return grossprofit_margin


def quality_low_leverage(debt_to_assets: pd.Series) -> pd.Series:
    """低杠杆质量因子：资产负债率取负号，让"越大越好"的符号约定保持一致
    （杠杆越低通常被质量投资视为更优质，尽管该假设本身需要因子单测验证）。"""
    return -debt_to_assets


def quality_earnings_growth(netprofit_yoy: pd.Series) -> pd.Series:
    return netprofit_yoy


def compute_value_quality_factors(df: pd.DataFrame) -> pd.DataFrame:
    """给一个截面（或面板）追加价值/质量因子列，输入列名对齐 panel.py 产出的面板列名。"""
    out = df.copy()
    if "pb" in out.columns:
        out["factor_bp"] = value_bp(out["pb"])
    if "pe_ttm" in out.columns:
        out["factor_ep"] = value_ep(out["pe_ttm"])
    if "ps_ttm" in out.columns:
        out["factor_sp"] = value_sp(out["ps_ttm"])
    if "total_mv" in out.columns:
        out["factor_size"] = size_log_mktcap(out["total_mv"])
    if "roe" in out.columns:
        out["factor_roe"] = quality_roe(out["roe"])
    if "grossprofit_margin" in out.columns:
        out["factor_gross_margin"] = quality_gross_margin(out["grossprofit_margin"])
    if "debt_to_assets" in out.columns:
        out["factor_low_leverage"] = quality_low_leverage(out["debt_to_assets"])
    if "netprofit_yoy" in out.columns:
        out["factor_earnings_growth"] = quality_earnings_growth(out["netprofit_yoy"])
    return out
