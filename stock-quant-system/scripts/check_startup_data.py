"""本机启动时快速检查灌库状态，避免遗漏日更/周更。

不自动 spawn 灌库（避免登录即占写锁）；需要时用 ensure_data_fresh --apply。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.db import get_db_path, get_ui_connection, init_schema
from common.warehouse_readiness import assess_warehouse_readiness, ingestion_update_in_progress


def main() -> int:
    if not get_db_path().is_file():
        print("BLOCK: 仓库不存在 → python -m scripts.run_daily_refresh --profile bootstrap")
        return 2

    if ingestion_update_in_progress():
        print("BUSY: 灌库进行中 → python -m scripts.watch_data_update --interval 30")
        return 3

    conn = get_ui_connection()
    try:
        init_schema(conn)
        report = assess_warehouse_readiness(conn)
    finally:
        conn.close()

    for w in report.warnings:
        print(f"WARN: {w}")
    if report.ready:
        eff = report.metrics.get("effective_quote_date", "—")
        print(f"OK: 投产就绪，有效截面日 {eff}")
        return 0

    print("BLOCK: 未满足投产门槛：")
    for b in report.blockers:
        print(f"  - {b}")
    print("修复: python -m scripts.ensure_data_fresh --apply --profile auto")
    print("      （工作日 weekday_decision / 周末 weekend_research / 空库 bootstrap）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
