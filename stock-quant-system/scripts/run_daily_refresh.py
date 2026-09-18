"""收盘后/日常数据刷新预设（封装 run_data_update 参数）。

A 股决策链是日频，不做 tick 推送；本入口负责 **灌库新鲜度**。
"""
from __future__ import annotations

import argparse
import logging
import sys

from common.ingestion_engine.orchestrator import build_arg_parser, run_data_update

PROFILE_ARGV: dict[str, list[str]] = {
    # 全量灌库：跳过日更轻量步（北交所由 tushare_prices、决策量价由全量 quotes 覆盖）
    "bootstrap": ["--fresh-run", "--skip", "quotes_decision,tushare_bse_quotes"],
    "incremental": [
        "--skip",
        "fundamentals,income_statement,corporate_actions,quotes_decision,tushare_bse_quotes",
    ],
    # 工作日：持仓/待办量价 + 全市场涨跌停/市值截面（R3）
    "weekday_decision": [
        "--only",
        "universe,quotes_decision,market_data,tushare_market_data",
    ],
    # 周末：全市场行情补齐 + 财务/行为 + 复权/北交所
    "weekend_research": [
        "--only",
        "quotes,fundamentals,income_statement,corporate_actions,reference_data,tushare_prices,tushare_market_data,tushare_behavior",
    ],
    "decision_min": [
        "--only",
        "universe,quotes_decision,market_data,tushare_market_data",
    ],
}


def main() -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--profile",
        choices=[*PROFILE_ARGV.keys(), "index_weight_gap"],
        default="incremental",
    )
    pre_args, remaining = pre.parse_known_args()

    if pre_args.profile == "index_weight_gap":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        from ingestion.tushare_market_data import sync_index_weight

        print(sync_index_weight(limit=None))
        return

    argv = PROFILE_ARGV[pre_args.profile] + remaining
    parser = build_arg_parser("日频数据刷新")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("run_daily_refresh").info("profile=%s", pre_args.profile)
    print(run_data_update(args))


if __name__ == "__main__":
    main()
