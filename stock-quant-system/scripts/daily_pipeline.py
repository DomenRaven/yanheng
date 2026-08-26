"""
全模块统一增量更新入口（Phase 0-0.7 完成后的日常/每周运行唯一入口）。

2026-08-26 重写：此前版本只覆盖 universe/quotes_batch/fundamentals_batch 三步，
是 Phase 0 早期的极简版本。现在项目已有10个 ingestion 模块，
且 Phase 0.5/0.6/0.7 的补抓脚本（parallel_sidecars.py/resume_tushare_behavior.py/
retry_corporate_actions.py/auto_chain_runner.py）都是当时为了绕开单写者限制、
并行抓多个数据源专门搭的临时编排，任务完成后已不再需要——本脚本合并它们的职责，
串行调用全部 ingestion 模块的增量同步函数（DuckDB 单写者，不能并行写），
作为今后唯一的定时任务入口。

步骤顺序（体现依赖关系：universe先于所有按股票遍历的任务；行情先于财务，
因为财务/因子计算要用到daily_basic.total_mv做市值）：
    1. universe              股票池同步（新股/退市/行业分类）
    2. quotes_batch           行情历史增量更新（AKShare 全市场日线）
    3. fundamentals_batch      财务比率指标（新浪，沪深）+ 北交所（Tushare补齐）
    4. income_statement_batch  利润表绝对值（Tushare，含北交所）
    5. reference_data          披露日历 + 退市股票池 + 行业分类
    6. market_data             交易日历 + 指数行情 + 指数成分
    7. corporate_actions       股本变动 + 分红历史
    8. tushare_prices          复权因子 + 退市股行情 + 北交所/CDR前复权行情
    9. tushare_market_data     daily_basic + 指数权重 + 停牌日历 + 涨跌停价
    10. tushare_behavior        龙虎榜/大宗交易/融资融券/资金流/北向资金等行为金融数据

每步独立 try/except：单个数据源当天抓取失败不应阻塞其余步骤，失败步骤记录在
最终汇总里，下次运行会自然重试（增量逻辑保证不会因为跳过一天而丢数据）。

用法：
    python -m scripts.daily_pipeline                      # 全量流程（生产/定时任务用）
    python -m scripts.daily_pipeline --only quotes,tushare_prices   # 只跑指定步骤（调试/补抓用）
    python -m scripts.daily_pipeline --skip fundamentals,income_statement  # 跳过指定步骤（财务数据更新频率低，可只每周跑一次）
"""
from __future__ import annotations

import argparse
import logging
import time

logger = logging.getLogger("daily_pipeline")

_STEP_ORDER = [
    "universe",
    "quotes",
    "fundamentals",
    "income_statement",
    "reference_data",
    "market_data",
    "corporate_actions",
    "tushare_prices",
    "tushare_market_data",
    "tushare_behavior",
]


def _run_universe() -> dict:
    from ingestion.universe import sync_universe

    df = sync_universe()
    return {"rows": len(df)}


def _run_quotes(limit: int | None) -> dict:
    from ingestion.quotes_batch import sync_quotes_batch

    return sync_quotes_batch(limit=limit)


def _run_fundamentals(limit: int | None) -> dict:
    from ingestion.fundamentals_batch import sync_fundamentals_batch, sync_fundamentals_bse_tushare

    sina_stats = sync_fundamentals_batch(limit=limit)
    bse_stats = sync_fundamentals_bse_tushare(limit=limit)
    return {"sina": sina_stats, "bse_tushare": bse_stats}


def _run_income_statement(limit: int | None) -> dict:
    from ingestion.income_statement_batch import sync_income_statement_batch

    return sync_income_statement_batch(limit=limit, source="tushare")


def _run_reference_data() -> dict:
    from ingestion.reference_data import sync_all_reference_data

    return sync_all_reference_data()


def _run_market_data() -> dict:
    from ingestion.market_data import sync_all_market_data

    return sync_all_market_data()


def _run_corporate_actions(limit: int | None) -> dict:
    from ingestion.corporate_actions import sync_corporate_actions

    return sync_corporate_actions(limit=limit)


def _run_tushare_prices() -> dict:
    from ingestion.tushare_prices import sync_all_tushare_prices

    return sync_all_tushare_prices()


def _run_tushare_market_data() -> dict:
    from ingestion.tushare_market_data import sync_all_tushare_market_data

    return sync_all_tushare_market_data()


def _run_tushare_behavior() -> dict:
    from ingestion.tushare_behavior import sync_all_tushare_behavior

    return sync_all_tushare_behavior()


_STEP_FUNCS = {
    "universe": lambda args: _run_universe(),
    "quotes": lambda args: _run_quotes(args.quotes_limit),
    "fundamentals": lambda args: _run_fundamentals(args.fundamentals_limit),
    "income_statement": lambda args: _run_income_statement(args.fundamentals_limit),
    "reference_data": lambda args: _run_reference_data(),
    "market_data": lambda args: _run_market_data(),
    "corporate_actions": lambda args: _run_corporate_actions(args.corporate_actions_limit),
    "tushare_prices": lambda args: _run_tushare_prices(),
    "tushare_market_data": lambda args: _run_tushare_market_data(),
    "tushare_behavior": lambda args: _run_tushare_behavior(),
}


def run_pipeline(args: argparse.Namespace) -> dict:
    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()

    steps = [s for s in _STEP_ORDER if (only is None or s in only) and s not in skip]
    results: dict[str, dict] = {}
    t0 = time.time()

    for i, step in enumerate(steps):
        logger.info("=== Step %d/%d: %s ===", i + 1, len(steps), step)
        t_step = time.time()
        try:
            stats = _STEP_FUNCS[step](args)
            results[step] = {"status": "ok", "stats": stats, "elapsed_s": round(time.time() - t_step, 1)}
            logger.info("%s 完成，耗时 %.1fs: %s", step, time.time() - t_step, stats)
        except Exception as exc:  # noqa: BLE001 - 单步失败不阻塞其余步骤，下次增量运行会自然重试
            results[step] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            logger.exception("%s 失败，跳过，下次运行会重试", step)

    logger.info(
        "daily_pipeline 全部完成，耗时 %.1f 分钟，%d/%d 步骤成功",
        (time.time() - t0) / 60,
        sum(1 for r in results.values() if r["status"] == "ok"),
        len(steps),
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="全模块统一增量更新入口")
    parser.add_argument("--only", type=str, default=None, help=f"逗号分隔，只运行指定步骤。可选：{','.join(_STEP_ORDER)}")
    parser.add_argument("--skip", type=str, default=None, help="逗号分隔，跳过指定步骤")
    parser.add_argument("--quotes-limit", type=int, default=None, help="调试用：行情只同步前N只股票")
    parser.add_argument("--fundamentals-limit", type=int, default=None, help="调试用：财务数据只同步前N只股票")
    parser.add_argument("--corporate-actions-limit", type=int, default=None, help="调试用：公司行为只同步前N只股票")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    results = run_pipeline(args)
    print(results)


if __name__ == "__main__":
    main()
