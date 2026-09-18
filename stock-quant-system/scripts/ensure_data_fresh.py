"""若库未就绪则启动数据刷新（子进程，不占用 Streamlit 写锁）。

默认只检查并打印；加 --apply 且在无进行中的 run 时 spawn run_daily_refresh。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.db import get_db_path, get_ui_connection, init_schema
from common.warehouse_readiness import assess_warehouse_readiness, ingestion_update_in_progress


def main() -> int:
    parser = argparse.ArgumentParser(description="检查并在需要时启动日频数据刷新")
    parser.add_argument("--apply", action="store_true", help="未就绪且未在更新时启动子进程灌库")
    parser.add_argument(
        "--profile",
        default="auto",
        choices=["auto", "bootstrap", "incremental", "decision_min", "weekday_decision", "weekend_research"],
    )
    args = parser.parse_args()

    if not get_db_path().is_file():
        report_blockers = ["仓库文件不存在"]
        profile = "bootstrap"
    else:
        conn = get_ui_connection()
        try:
            init_schema(conn)
            report = assess_warehouse_readiness(conn)
        finally:
            conn.close()
        if report.ready:
            print("OK: 仓库已满足投产门槛")
            return 0
        report_blockers = report.blockers
        profile = args.profile
        if profile == "auto":
            import datetime as dt

            uni = report.metrics.get("universe_active") or 0
            if uni < 3000 or report.metrics.get("effective_quote_date") is None:
                profile = "bootstrap"
            elif dt.date.today().weekday() >= 5:
                profile = "weekend_research"
            else:
                profile = "weekday_decision"

    print("NOT READY:")
    for b in report_blockers:
        print(" ", b)

    if not args.apply:
        print("\n加 --apply 可在后台启动: python -m scripts.run_daily_refresh --profile", profile)
        return 1

    if ingestion_update_in_progress():
        print("已有流水线断点/运行中，请: python -m scripts.run_data_update --resume")
        return 1

    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = Path(sys.executable)
    cmd = [str(py), "-m", "scripts.run_daily_refresh", "--profile", profile]
    log_dir = _ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ensure_data_fresh_{profile}.log"
    print("启动:", " ".join(cmd), "日志:", log_path)
    with open(log_path, "a", encoding="utf-8") as logf:
        subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    print("已在后台启动；监测: python -m scripts.watch_data_update")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
