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
