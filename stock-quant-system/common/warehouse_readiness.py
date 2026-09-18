"""生产库就绪评估：空库 / 假新 / 缺涨跌停等一律视为投产阻塞。

A 股日频产品不做 tick 实时；「新鲜度」= 有效全市场截面日对齐最近交易日 + 决策链依赖表覆盖。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from common.config import get_config


@dataclass
class WarehouseReadinessReport:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    recommended_command: str = ""
    update_in_progress: bool = False


def _cfg() -> dict[str, Any]:
    return get_config().get("production_readiness") or {}


def _active_universe_count(conn) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM universe WHERE is_delisted = FALSE"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def effective_market_quote_date(conn) -> dt.date | None:
    """与 research/panel.build_asof_snapshot 同口径：≥80% 活跃 universe 有 qfq 行情的最近一日。"""
    try:
        row = conn.execute(
            """
            SELECT trade_date FROM (
                SELECT trade_date, count(*) AS n FROM daily_quotes
                WHERE adjust = 'qfq'
                GROUP BY trade_date
            )
            WHERE n >= (SELECT count(*) * 0.8 FROM universe WHERE is_delisted = FALSE)
            ORDER BY trade_date DESC LIMIT 1
            """
        ).fetchone()
        if row and row[0] is not None:
            return pd_timestamp_to_date(row[0])
    except Exception:
        pass
    return None


def pd_timestamp_to_date(v: object) -> dt.date:
    import pandas as pd

    return pd.Timestamp(v).date()


def expected_latest_trade_date(conn, as_of: dt.date | None = None) -> dt.date | None:
    as_of = as_of or dt.date.today()
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM trade_calendar WHERE trade_date <= ?",
            [as_of],
        ).fetchone()
        if row and row[0] is not None:
            return pd_timestamp_to_date(row[0])
    except Exception:
        pass
    # 日历未灌：工作日近似（不含法定假日，仅兜底）
    d = as_of
    for _ in range(10):
        if d.weekday() < 5:
            return d
        d -= dt.timedelta(days=1)
    return None


def trading_days_between(conn, start: dt.date, end: dt.date) -> int | None:
    if start >= end:
        return 0
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM trade_calendar
            WHERE trade_date > ? AND trade_date <= ?
            """,
            [start, end],
        ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def ingestion_update_in_progress() -> bool:
    from common.ingestion_engine.pipeline_state import load_state

    state = load_state()
    if not state:
        return False
    return state.get("status") in ("running", "interrupted")


def recommended_refresh_command(*, empty_bootstrap: bool) -> str:
    if empty_bootstrap:
        return (
            "cd stock-quant-system\n"
            ".venv\\Scripts\\python.exe -m scripts.run_daily_refresh --profile bootstrap"
        )
    return (
        "cd stock-quant-system\n"
        ".venv\\Scripts\\python.exe -m scripts.run_daily_refresh --profile incremental"
    )


def assess_warehouse_readiness(conn) -> WarehouseReadinessReport:
    cfg = _cfg()
    min_cov = float(cfg.get("min_quote_coverage_ratio", 0.80))
    max_lag = int(cfg.get("max_quote_lag_trading_days", 2))
    min_limit = float(cfg.get("min_limit_price_coverage", 0.90))
    min_basic = float(cfg.get("min_daily_basic_coverage", 0.80))
    min_uni = int(cfg.get("min_universe_symbols", 3000))

    report = WarehouseReadinessReport(ready=False)
    report.update_in_progress = ingestion_update_in_progress()
    if report.update_in_progress:
        report.warnings.append("检测到数据更新正在进行或曾中断。请等待更新完成，或按运维说明继续未完成的更新后再生成建议。")

    uni_n = _active_universe_count(conn)
    report.metrics["universe_active"] = uni_n
    if uni_n < min_uni:
        report.blockers.append(
            f"股票池不足（活跃 {uni_n} 只，门槛 ≥{min_uni}）。请先完成股票池与行情数据更新。"
        )

    eff = effective_market_quote_date(conn)
    report.metrics["effective_quote_date"] = str(eff) if eff else None
    naive_max = conn.execute(
        "SELECT MAX(trade_date) FROM daily_quotes WHERE adjust='qfq'"
    ).fetchone()
    report.metrics["naive_max_quote_date"] = (
        str(pd_timestamp_to_date(naive_max[0])) if naive_max and naive_max[0] else None
    )

    if eff is None:
        report.blockers.append(
            "缺少有效的全市场前复权日线（库为空，或覆盖不足活跃股票的 80%）。暂不可生成建议。"
        )
        report.recommended_command = recommended_refresh_command(empty_bootstrap=True)
        report.ready = False
        return report

    # 假新：naive max 比 effective 新但覆盖不够
    if report.metrics["naive_max_quote_date"] and report.metrics["naive_max_quote_date"] != report.metrics[
        "effective_quote_date"
    ]:
        report.warnings.append(
            f"存在「仅部分股票更新」的较新日期 {report.metrics['naive_max_quote_date']}；"
            f"生成建议时使用有效截面日 {report.metrics['effective_quote_date']}。"
        )

    on_day = conn.execute(
        """
        SELECT COUNT(*) FROM daily_quotes
        WHERE adjust='qfq' AND trade_date = ?
        """,
        [eff],
    ).fetchone()[0]
    cov = on_day / uni_n if uni_n else 0.0
    report.metrics["quote_coverage_ratio"] = round(cov, 4)
    if cov < min_cov:
        report.blockers.append(
            f"有效截面日 {eff} 行情覆盖率 {cov:.1%} < {min_cov:.0%}。"
        )

    exp = expected_latest_trade_date(conn)
    report.metrics["expected_latest_trade_date"] = str(exp) if exp else None
    if exp:
        lag = trading_days_between(conn, eff, exp)
        if lag is None:
            lag = (exp - eff).days
        report.metrics["quote_lag_trading_days"] = lag
        if lag > max_lag:
            report.blockers.append(
                f"行情有效截面 {eff} 落后最近交易日 {exp} 共 {lag} 个交易日（门槛 ≤{max_lag}）。"
                "请跑收盘后增量更新。"
            )

    try:
        lp = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM limit_price WHERE trade_date = ?) AS lp,
              (SELECT COUNT(*) FROM daily_quotes WHERE adjust='qfq' AND trade_date = ?) AS dq
            """,
            [eff, eff],
        ).fetchone()
        lp_n, dq_n = int(lp[0]), int(lp[1])
        lp_cov = lp_n / dq_n if dq_n else 0.0
        report.metrics["limit_price_coverage"] = round(lp_cov, 4)
        if dq_n > 0 and lp_cov < min_limit:
            report.blockers.append(
                f"涨跌停价覆盖率 {lp_cov:.1%} < {min_limit:.0%}（截面日 {eff}）。"
                "请完成含涨跌停价的市场数据更新。"
            )
    except Exception:
        report.blockers.append("无法读取涨跌停价表（请先完成市场数据更新）。")

    try:
        basic_n = conn.execute(
            "SELECT COUNT(*) FROM daily_basic WHERE trade_date = ?",
            [eff],
        ).fetchone()[0]
        basic_cov = int(basic_n) / uni_n if uni_n else 0.0
        report.metrics["daily_basic_coverage"] = round(basic_cov, 4)
        if basic_cov < min_basic:
            report.blockers.append(
                f"市值基本面覆盖率 {basic_cov:.1%} < {min_basic:.0%}（扫描与排序依赖此项）。"
            )
    except Exception:
        report.blockers.append("无法读取市值基本面表（扫描与排序依赖此项）。")

    try:
        cal_n = conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
        report.metrics["trade_calendar_rows"] = int(cal_n)
        if int(cal_n) < 100:
            report.warnings.append("交易日历行数偏少，明日待办与交易日推算可能不准；请完成市场数据更新。")
    except Exception:
        report.warnings.append("交易日历不可用。")

    report.recommended_command = recommended_refresh_command(
        empty_bootstrap=eff is None or uni_n < min_uni
    )
    report.ready = len(report.blockers) == 0
    return report
