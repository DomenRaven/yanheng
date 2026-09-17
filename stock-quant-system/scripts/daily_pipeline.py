"""
全模块统一增量更新入口（Phase 0-0.7 完成后的日常/每周运行）。

自 2026-09 起编排委托 `common.ingestion_engine.orchestrator`；推荐使用
`python -m scripts.run_data_update` 作为数据更新专用入口（名称更贴切）。
两者步骤与参数等价。

用法：
    python -m scripts.daily_pipeline
    python -m scripts.run_data_update --only quotes,tushare_prices
"""
from __future__ import annotations

import logging

from common.ingestion_engine.orchestrator import build_arg_parser, run_data_update


def main() -> None:
    parser = build_arg_parser("全模块统一增量更新（兼容入口）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    results = run_data_update(args)
    print(results)


if __name__ == "__main__":
    main()
