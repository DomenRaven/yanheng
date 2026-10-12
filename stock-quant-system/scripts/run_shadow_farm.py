"""机器影子仓农场 CLI：供计划任务 / 手动验收调用。

用法：
  .venv\\Scripts\\python.exe -m scripts.run_shadow_farm
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from advice.shadow_farm import report_to_markdown, run_shadow_farm_once  # noqa: E402

_LOG = logging.getLogger("scripts.run_shadow_farm")
_WAIT_S = 3 * 3600
_POLL_S = 20.0


def pipeline_blocks_shadow(state: dict | None) -> bool:
    """灌库断点仍是 running 时，影子仓不能抢 DuckDB 写锁。"""
    return bool(state and state.get("status") == "running")


def wait_for_shadow_window(*, max_wait_s: float = _WAIT_S, poll_s: float = _POLL_S) -> bool:
    """00:30 灌库和 03:00 影子仓在开机后会一起补跑。等到灌库结束并且写锁能打开。"""
    import time

    from common.db import WarehouseBusyError, get_connection
    from common.ingestion_engine.pipeline_state import clear_stale_pipeline_lock, load_state

    deadline = time.monotonic() + max_wait_s
    while True:
        clear_stale_pipeline_lock()
        state = load_state()
        if pipeline_blocks_shadow(state):
            _LOG.info("灌库尚未结束 run_id=%s，影子仓继续等", (state or {}).get("run_id"))
        else:
            try:
                conn = get_connection(read_only=False, retries=2)
                conn.close()
                return True
            except WarehouseBusyError:
                _LOG.info("仓库写锁仍被占用，影子仓继续等")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _LOG.error("等待灌库结束超过 %.0f 秒，本次影子仓不记账", max_wait_s)
            return False
        time.sleep(min(poll_s, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run YanHeng shadow farm once")
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="日历日 YYYY-MM-DD（默认今天；净值回退到最近有行情日）",
    )
    parser.add_argument(
        "--open-cohort",
        action="store_true",
        help="强制确保本周 8 户队列（周末链路默认会开；日常一般可省略）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else None
    if not wait_for_shadow_window():
        return 1
    report = run_shadow_farm_once(
        as_of=as_of,
        # 日常计划任务默认只盯市；周末链路 / --open-cohort 才换票
        open_new_cohort=True if args.open_cohort else False,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("\n" + report_to_markdown(report))
    return 0 if report.get("status") in ("ok", "skipped_data") else 1


if __name__ == "__main__":
    raise SystemExit(main())
