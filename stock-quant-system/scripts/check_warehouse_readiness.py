"""检查生产库是否满足投产门槛；非 0 退出码供 CI/启动前脚本使用。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.db import get_db_path, get_ui_connection, init_schema
from common.warehouse_readiness import assess_warehouse_readiness


def main() -> int:
    path = get_db_path()
    if not path.is_file():
        print(f"BLOCK: 仓库不存在 {path}")
        print("建议: python -m scripts.run_daily_refresh --profile bootstrap")
        return 1

    conn = get_ui_connection()
    try:
        init_schema(conn)
        report = assess_warehouse_readiness(conn)
    finally:
        conn.close()

    print("ready:", report.ready)
    for k, v in report.metrics.items():
        print(f"  {k}: {v}")
    if report.update_in_progress:
        print("  update_in_progress: True")
    if report.warnings:
        print("warnings:")
        for w in report.warnings:
            print(" ", w)
    if report.blockers:
        print("blockers:")
        for b in report.blockers:
            print(" ", b)
        print("\n建议命令:\n", report.recommended_command)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
