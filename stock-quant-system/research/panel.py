"""
Point-in-time 特征面板构建（Phase 1）。打开 warehouse.duckdb，是 research 包里
唯一"摸库"的模块——量价/价值/质量因子的纯计算逻辑仍在 factors.py，本模块只负责
"从数据库里取出正确的、不含未来函数的数字"这一件事。

截面频率：月末（月度再平衡）。原因：
  - fundamentals/income_statement 本身是季度更新，日度重算意义不大；
  - 5549只股票 x ~2870个交易日的全量日度面板对个人PC内存不现实，
    月度面板约5549 x 140个月 ≈ 78万行，量级可控，且足以覆盖
    Purged K-Fold / Walk-Forward 所需的时序长度（10年+，跨多个牛熊周期）。

Point-in-time对齐规则（对齐 docs/phase0-acceptance-report.md 缺口3的工程决策）：
  - 价值因子（BP/EP/SP/规模）：直接用 daily_basic 当日官方值，Tushare按当日已公开
    的股本+当日收盘价计算，天然 point-in-time，无需额外对齐。
  - 质量因子（ROE等比率）：fundamentals EAV表按 report_date 只是"报告期"，
    必须用 disclosure_calendar.announce_date 才知道"哪天才真正公开"；
    disclosure_calendar 缺失时按保守固定滞后规则兜底（季报T+45日/年报T+60日，
    覆盖A股法定披露窗口）。用 DuckDB ASOF JOIN 取"截面日之前最后一次已公开"的值。

可投资域过滤：exchange IN ('sh','sz','bj') AND is_delisted=FALSE 已经天然排除了
28只 exchange='unknown' 的已退市B股（缺口4，见验收报告）；再叠加
research/a_share_rules.is_too_new 排除次新股（默认上市不满60自然日，
避免上市初期无量价历史/交易制度特殊期污染因子）。
"""
from __future__ import annotations

import datetime as dt
import logging

import duckdb
import pandas as pd

from research.a_share_rules import is_too_new

logger = logging.getLogger("research.panel")

# fundamentals EAV -> 因子计算用的英文列名，只取"质量因子必需最小集"
# （见 ingestion/fundamentals_batch.py 里为北交所打的同一套映射，两边字符串必须一致）
_QUALITY_INDICATOR_MAP = {
    "净资产收益率(%)": "roe",
    "加权净资产收益率(%)": "roe_waa",
    "总资产净利润率(%)": "roa",
    "销售毛利率(%)": "grossprofit_margin",
    "销售净利率(%)": "netprofit_margin",
    "资产负债率(%)": "debt_to_assets",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "总资产周转率(次)": "assets_turn",
    "应收账款周转率(次)": "ar_turn",
    "每股净资产_调整后(元)": "bps",
    "净利润增长率(%)": "netprofit_yoy",
    "主营业务收入增长率(%)": "or_yoy",
    "净资产增长率(%)": "equity_yoy",
    "摊薄每股收益(元)": "dt_eps",
}


def _norm_date(s: str) -> str:
    """接受 YYYYMMDD 或 YYYY-MM-DD，统一转成 DuckDB 认识的 YYYY-MM-DD。"""
    return pd.Timestamp(s).strftime("%Y-%m-%d")


def get_rebalance_dates(conn: duckdb.DuckDBPyConnection, start_date: str, end_date: str) -> list[dt.date]:
    """每月最后一个交易日，从 trade_calendar 取（不是自然日月末，避免非交易日）。"""
    start_date, end_date = _norm_date(start_date), _norm_date(end_date)
    df = conn.execute(
        """
        SELECT trade_date FROM (
            SELECT trade_date,
                   row_number() OVER (PARTITION BY year(trade_date), month(trade_date) ORDER BY trade_date DESC) AS rn
            FROM trade_calendar
            WHERE trade_date BETWEEN ? AND ?
        ) WHERE rn = 1
        ORDER BY trade_date
        """,
        [start_date, end_date],
    ).df()
    return list(df["trade_date"])


def _load_investable_universe(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """全历史候选池（含已退市，退市日期用于面板期内动态判断是否在池），
    真正"是否可投资"的时间窗口过滤在 build_feature_panel 里按截面日逐个判断。

    2026-08-26 补充：显式排除 is_st（含*ST）。config.yaml 里 exclude_st:false
    是"数据层不过滤，交由策略层决定"的显式约定——本模块就是那个"策略层"。
    第一版建完模型后用 advice/scanner.py 抽查发现Top候选被*ST股霸屏（*ST股
    因财务危机/摘牌风险导致的低价、剧烈波动、扭曲的账面指标，容易被BP/ROE等
    价值质量因子误判为"便宜的优质股"，是A股量化选股的已知陷阱），因此在此
    补上排除，而不是留给用户在生产环境里踩坑后才发现。

    已知局限：universe.is_st 只是"当前"标记，不是历史时点标记（schema里没有
    ST状态变更历史表），意味着"曾经ST过、现已摘帽"的股票历史区间也会被本函数
    误排除；"当前非ST、历史上曾是ST"的区间则无法排除——用当前状态近似历史状态，
    是本版本的已知简化，更精细的时点ST历史留给后续版本。"""
    return conn.execute(
        """
        SELECT symbol, exchange, board, is_st, is_delisted, list_date, delist_date
        FROM universe
        WHERE exchange IN ('sh', 'sz', 'bj') AND is_st = FALSE
        """
    ).df()


def _load_price_factor_history(conn: duckdb.DuckDBPyConnection, start_date: str) -> pd.DataFrame:
    """全历史量价因子，用SQL窗口函数一次性算好（比逐股票Python循环快得多）。
    只从 start_date 前 400 个自然日开始拉（覆盖 mom_12_1 的252+21日回看窗），
    实际返回列会在调用侧再筛选到截面日。"""
    lookback_start = (pd.Timestamp(_norm_date(start_date)) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    return conn.execute(
        """
        WITH base AS (
            SELECT
                symbol, trade_date, close, high, low, turnover,
                close / NULLIF(LAG(close, 1) OVER w, 0) - 1 AS daily_ret,
                close / NULLIF(LAG(close, 21) OVER w, 0) - 1 AS factor_rev_1m,
                LAG(close, 21) OVER w / NULLIF(LAG(close, 252) OVER w, 0) - 1 AS factor_mom_12_1
            FROM daily_quotes
            WHERE adjust = 'qfq' AND trade_date >= ?
            WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
        )
        SELECT
            symbol, trade_date, close, factor_rev_1m, factor_mom_12_1,
            STDDEV_SAMP(daily_ret) OVER (
                PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS factor_vol_20,
            AVG(turnover) OVER (
                PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS factor_turnover_20,
            AVG((high - low) / NULLIF(close, 0)) OVER (
                PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
            ) AS factor_amp_20
        FROM base
        """,
        [lookback_start],
    ).df()


def _load_daily_basic_asof(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT symbol, trade_date, pb, pe_ttm, ps_ttm, total_mv, circ_mv, turnover_rate
        FROM daily_basic
        """
    ).df()


def _load_industry_classification(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """申万行业分类变动历史，point-in-time（start_date=生效起始日）。Phase3的GNN
    挑战者(research/train_dl.py)用它构建"同行业"关系图的边；本身对因子计算无影响，
    加在这里而不是train_dl.py自己摸库，是为了遵守"panel.py是research包唯一摸库模块"
    的既定分层约定。"""
    return conn.execute("SELECT symbol, start_date, industry_code FROM industry_classification").df()


def _load_fundamentals_pit(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """fundamentals EAV -> 宽表 pivot，附加 effective_date（point-in-time对齐后的
    "该报告期数据从哪天起才算已公开"）。"""
    case_cols = ",\n            ".join(
        f"MAX(CASE WHEN indicator = '{cn}' THEN value END) AS {en}"
        for cn, en in _QUALITY_INDICATOR_MAP.items()
    )
    sql = f"""
        WITH ind AS (
            SELECT symbol, report_date,
            {case_cols}
            FROM fundamentals
            GROUP BY symbol, report_date
        )
        SELECT
            ind.*,
            COALESCE(
                dc.announce_date,
                ind.report_date + CASE WHEN month(ind.report_date) = 12 THEN INTERVAL 60 DAY ELSE INTERVAL 45 DAY END
            ) AS effective_date
        FROM ind
        LEFT JOIN disclosure_calendar dc
            ON dc.symbol = ind.symbol AND dc.report_date = ind.report_date
    """
    return conn.execute(sql).df()


def _build_panel_for_dates(
    section_dates: list[dt.date],
    lookback_start: str,
    min_listed_days: int = 60,
) -> pd.DataFrame:
    """真正的面板构建核心：给定任意一组截面日（可以是历史月末，也可以是"今天"
    这一个日期），拼出 point-in-time 特征面板。build_feature_panel（历史面板，
    训练/因子评估用）和 build_asof_snapshot（当前快照，scanner.py 用）
    都只是"算出该传哪组日期"，实际取数逻辑统一在这里，避免两处实现漂移。"""
    from common.db import get_connection

    conn = get_connection()
    try:
        universe = _load_investable_universe(conn)
        price_hist = _load_price_factor_history(conn, lookback_start)
        daily_basic = _load_daily_basic_asof(conn)
        fund_pit = _load_fundamentals_pit(conn)
        industry_hist = _load_industry_classification(conn)
    finally:
        conn.close()

    section_df = pd.DataFrame({"trade_date": section_dates})

    price_panel = price_hist.merge(section_df, on="trade_date", how="inner")
    value_panel = daily_basic.merge(section_df, on="trade_date", how="inner")

    panel = price_panel.merge(value_panel, on=["symbol", "trade_date"], how="left")

    panel = panel.merge(
        universe[["symbol", "exchange", "board", "is_st", "is_delisted", "list_date", "delist_date"]],
        on="symbol",
        how="inner",
    )

    # 可投资性过滤：截面日当天已上市满min_listed_days，且尚未退市（或退市日在截面日之后）
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.date
    panel["list_date"] = pd.to_datetime(panel["list_date"]).dt.date
    panel["delist_date"] = pd.to_datetime(panel["delist_date"]).dt.date
    not_too_new = ~panel.apply(
        lambda r: is_too_new(
            r["list_date"] if pd.notna(r["list_date"]) else None, r["trade_date"], min_listed_days
        ),
        axis=1,
    )
    not_yet_delisted = panel["delist_date"].isna() | (panel["delist_date"] > panel["trade_date"])
    panel = panel[not_too_new & not_yet_delisted].copy()

    # 质量因子：ASOF JOIN，每个(symbol, trade_date)取截面日之前最后一次已公开的报告期
    con = duckdb.connect()
    con.register("panel_dates", panel[["symbol", "trade_date"]].drop_duplicates())
    con.register("fund_pit", fund_pit)
    quality_asof = con.execute(
        """
        SELECT p.symbol, p.trade_date, f.* EXCLUDE (symbol, report_date, effective_date), f.report_date AS fund_report_date
        FROM panel_dates p
        ASOF LEFT JOIN fund_pit f
            ON f.symbol = p.symbol AND f.effective_date <= p.trade_date
        """
    ).df()
    con.close()
    quality_asof["trade_date"] = pd.to_datetime(quality_asof["trade_date"]).dt.date

    panel = panel.merge(quality_asof, on=["symbol", "trade_date"], how="left")

    # 行业分类：同一套ASOF JOIN方式，取截面日之前最后一次生效的行业代码
    con = duckdb.connect()
    con.register("panel_dates2", panel[["symbol", "trade_date"]].drop_duplicates())
    con.register("industry_hist", industry_hist)
    industry_asof = con.execute(
        """
        SELECT p.symbol, p.trade_date, i.industry_code
        FROM panel_dates2 p
        ASOF LEFT JOIN industry_hist i
            ON i.symbol = p.symbol AND i.start_date <= p.trade_date
        """
    ).df()
    con.close()
    industry_asof["trade_date"] = pd.to_datetime(industry_asof["trade_date"]).dt.date
    panel = panel.merge(industry_asof, on=["symbol", "trade_date"], how="left")

    from research.factors import compute_value_quality_factors

    panel = compute_value_quality_factors(panel)
    return panel.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def build_feature_panel(
    start_date: str = "20160101",
    end_date: str | None = None,
    min_listed_days: int = 60,
) -> pd.DataFrame:
    """主入口：返回月度截面面板。列：symbol, trade_date, close, 各 factor_* 列。

    每个 (symbol, trade_date) 只有截面日之前"已公开"的数据参与计算，
    不存在使用截面日之后才公开的信息的未来函数问题。
    """
    end_date = end_date or dt.date.today().strftime("%Y%m%d")
    from common.db import get_connection

    conn = get_connection()
    try:
        rebalance_dates = get_rebalance_dates(conn, start_date, end_date)
    finally:
        conn.close()
    if not rebalance_dates:
        raise ValueError(f"[{start_date}, {end_date}] 区间内没有月末交易日，检查trade_calendar是否已同步")
    logger.info("截面日期数: %d（%s ~ %s）", len(rebalance_dates), rebalance_dates[0], rebalance_dates[-1])
    return _build_panel_for_dates(rebalance_dates, start_date, min_listed_days)


def build_asof_snapshot(as_of_date: str | dt.date | None = None, min_listed_days: int = 60) -> pd.DataFrame:
    """给 advice/scanner.py 用：只取"最近一个已有行情的交易日"这一个截面，
    不要求是月末——每日跑都能拿到当天（或最近一个交易日）的全市场因子快照。"""
    from common.db import get_connection

    # 2026-08-26 查证：daily_quotes不同数据源(AKShare全市场批 vs Tushare北交所/CDR
    # 补抓)的最后更新日可能不同步（如某天只有BSE+CDR这339只更新到了，沪深主板还停
    # 在前一天），直接取全表max(trade_date)会拿到一个"看似最新但只覆盖6%股票"的
    # 假日期。这里改成"覆盖了至少80%活跃股票数的最近一个交易日"，更贴近实际语义
    # 上的"最近一个可用的全市场交易日"。
    conn = get_connection()
    try:
        cutoff = (
            f"AND trade_date <= '{_norm_date(as_of_date) if isinstance(as_of_date, str) else as_of_date}'"
            if as_of_date is not None
            else ""
        )
        latest = conn.execute(
            f"""
            SELECT trade_date FROM (
                SELECT trade_date, count(*) AS n FROM daily_quotes
                WHERE adjust = 'qfq' {cutoff}
                GROUP BY trade_date
            )
            WHERE n >= (SELECT count(*) * 0.8 FROM universe WHERE is_delisted = FALSE)
            ORDER BY trade_date DESC LIMIT 1
            """
        ).fetchone()
        latest = latest[0] if latest else None
    finally:
        conn.close()
    if latest is None:
        raise ValueError("daily_quotes 里没有可用的qfq行情，检查数据是否已同步")
    # lookback_start 传给 _load_price_factor_history 后它自己还会再往前减400天
    # （覆盖 mom_12_1 的252+21日回看窗），这里只需传"截面日"本身。
    logger.info("快照截面日: %s", latest)
    return _build_panel_for_dates([pd.Timestamp(latest)], str(latest), min_listed_days)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="构建point-in-time特征面板")
    parser.add_argument("--start", type=str, default="20160101")
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--out", type=str, default="data/feature_panel.parquet")
    args = parser.parse_args()

    df = build_feature_panel(args.start, args.end)
    logger.info("面板形状: %s，列: %s", df.shape, list(df.columns))
    df.to_parquet(args.out)
    logger.info("已写出 %s", args.out)
