"""
数据更新专用入口（并发取数 + 异步写库引擎）。

与 `scripts/daily_pipeline.py` 步骤集相同，默认启用 config.yaml 中
`ingestion.pipeline.async_write`。编排逻辑在 `common/ingestion_engine/orchestrator.py`。

用法：
    python -m scripts.run_data_update
    python -m scripts.run_data_update --only quotes,tushare_prices
    python -m scripts.run_data_update --skip fundamentals,income_statement
"""
from __future__ import annotations

import logging

from common.ingestion_engine.config import pipeline_async_write
from common.ingestion_engine.orchestrator import build_arg_parser, run_data_update


def main() -> None:
    parser = build_arg_parser("数据更新引擎（fetch/write 解耦）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("ingestion_engine").info("async_write=%s", pipeline_async_write())
    results = run_data_update(args)
    print(results)


if __name__ == "__main__":
    main()
