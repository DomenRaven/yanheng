"""周末双池掘金存档：先 hs，再 bj（无 bj 冠军则跳过并打印原因）。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.scanner import run_scan

logger = logging.getLogger("scripts.weekend_dual_scan")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    results = {}
    full, top, td = run_scan(top_n=50, pool_id="hs", source="weekend")
    results["hs"] = {"asof": str(td), "n": len(full), "top1": None if top.empty else top.iloc[0]["symbol"]}
    try:
        full_b, top_b, td_b = run_scan(top_n=50, pool_id="bj", source="weekend")
        results["bj"] = {
            "asof": str(td_b),
            "n": len(full_b),
            "top1": None if top_b.empty else top_b.iloc[0]["symbol"],
        }
    except FileNotFoundError as exc:
        results["bj"] = {"skipped": True, "reason": str(exc)}
        logger.warning("跳过 bj 扫描: %s", exc)
    print(results)


if __name__ == "__main__":
    main()
