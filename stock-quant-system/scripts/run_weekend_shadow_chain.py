"""周末无人值守链路：灌库成功 → 双池建议 → 影子仓。

供计划任务 ``StockQuant-WeekendResearch`` 调用，避免必须点 UI 才能换票。

用法::

  python -m scripts.run_weekend_shadow_chain
  python -m scripts.run_weekend_shadow_chain --skip-ingest   # 已灌过库时只建议+影子
  python -m scripts.run_weekend_shadow_chain --skip-shadow
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.ingestion_engine.orchestrator import build_arg_parser, run_data_update  # noqa: E402
from scripts.run_daily_refresh import PROFILE_ARGV  # noqa: E402

logger = logging.getLogger("scripts.weekend_shadow_chain")

POOLS = ("hs", "bj")


def _ingest_ok(results: dict[str, Any]) -> bool:
    steps = {k: v for k, v in results.items() if not str(k).startswith("_")}
    if not steps:
        return False
    return all(isinstance(v, dict) and v.get("status") == "ok" for v in steps.values())


def _clear_stale_pipeline_lock(*, max_age_hours: float = 2.0) -> dict[str, Any] | None:
    """清掉空步骤/过久的 running 断点，避免 ensure_data_fresh / UI 永久显示「更新中」。

    有真实步骤进度的断点不碰——应人工 ``--resume``。
    """
    from common.ingestion_engine.pipeline_state import load_state, mark_run_finished

    state = load_state()
    if not state or state.get("status") not in ("running", "interrupted"):
        return None
    steps = state.get("steps") or {}
    updated = state.get("updated_at") or state.get("started_at") or ""
    age_h: float | None = None
    try:
        # 2026-09-19T09:00:02+08:00
        ts = dt.datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        age_h = (dt.datetime.now(ts.tzinfo) - ts).total_seconds() / 3600.0
    except Exception:
        age_h = None

    empty = len(steps) == 0
    stale = age_h is not None and age_h >= max_age_hours
    if not (empty or stale):
        logger.warning(
            "检测到进行中的灌库断点 run_id=%s steps=%d age_h=%s，不自动清除",
            state.get("run_id"),
            len(steps),
            age_h,
        )
        return {"action": "kept", "run_id": state.get("run_id"), "n_steps": len(steps)}

    mark_run_finished(state, status="aborted_stale")
    logger.warning(
        "已清除陈旧灌库断点 run_id=%s empty=%s age_h=%s → aborted_stale",
        state.get("run_id"),
        empty,
        None if age_h is None else round(age_h, 2),
    )
    return {
        "action": "aborted_stale",
        "run_id": state.get("run_id"),
        "empty": empty,
        "age_h": None if age_h is None else round(age_h, 2),
    }


def _run_weekend_ingest() -> dict[str, Any]:
    argv = list(PROFILE_ARGV["weekend_research"])
    parser = build_arg_parser("周末无人值守灌库")
    args = parser.parse_args(argv)
    logger.info("weekend_research argv=%s", argv)
    return run_data_update(args)


def _generate_pool_advice(pool_id: str, *, top_n: int) -> dict[str, Any]:
    from advice.advice_engine import generate_daily_advice

    out = generate_daily_advice(top_n_watchlist=top_n, pool_id=pool_id)
    return {
        "status": "ok",
        "pool_id": pool_id,
        "as_of": out.get("as_of"),
        "n_position": len(out.get("position_cards") or []),
        "n_watchlist": len(out.get("watchlist_cards") or []),
        "n_todos": len(out.get("tomorrow_todos") or []),
    }


def _emit_report(report: dict[str, Any]) -> Path:
    out_dir = ROOT / "data" / "shadow_farm"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "latest_weekend_chain.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("===WEEKEND_SHADOW_CHAIN_REPORT===", flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    print(f"===REPORT_FILE={report_path}===", flush=True)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekend ingest → dual-pool advice → shadow farm")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="跳过 weekend_research（灌库已成功时用于补跑建议/影子）",
    )
    parser.add_argument(
        "--skip-advice",
        action="store_true",
        help="跳过双池 generate_daily_advice",
    )
    parser.add_argument(
        "--skip-shadow",
        action="store_true",
        help="跳过影子仓",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="观察名单长度（传给 generate_daily_advice）",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="影子仓日历日 YYYY-MM-DD（默认今天）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    report: dict[str, Any] = {
        "chain": "weekend_shadow",
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    exit_code = 0

    stale = _clear_stale_pipeline_lock()
    if stale:
        report["stale_pipeline"] = stale

    # --- 1) 灌库 ---
    if args.skip_ingest:
        report["ingest"] = {"status": "skipped"}
        logger.info("跳过 weekend_research（--skip-ingest）")
    else:
        ingest = _run_weekend_ingest()
        ok = _ingest_ok(ingest)
        report["ingest"] = {
            "status": "ok" if ok else "failed",
            "meta": ingest.get("_meta"),
            "steps_ok": ingest.get("_meta", {}).get("steps_ok"),
            "steps_total": ingest.get("_meta", {}).get("steps_total"),
        }
        if not ok:
            logger.error("weekend_research 未全部成功，中止建议与影子仓")
            report["advice"] = {"status": "aborted"}
            report["shadow"] = {"status": "aborted"}
            report["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            report["exit_code"] = 1
            _emit_report(report)
            return 1

    # --- 2) 双池建议 ---
    if args.skip_advice:
        report["advice"] = {"status": "skipped"}
        logger.info("跳过双池建议（--skip-advice）")
    else:
        advice_out: dict[str, Any] = {}
        any_ok = False
        for pool_id in POOLS:
            try:
                advice_out[pool_id] = _generate_pool_advice(pool_id, top_n=args.top_n)
                any_ok = True
                logger.info(
                    "建议完成 pool=%s as_of=%s todos=%s",
                    pool_id,
                    advice_out[pool_id].get("as_of"),
                    advice_out[pool_id].get("n_todos"),
                )
            except FileNotFoundError as exc:
                # 常见：缺 champion_bj
                advice_out[pool_id] = {"status": "skipped", "reason": str(exc)}
                logger.warning("跳过 pool=%s 建议: %s", pool_id, exc)
            except Exception as exc:
                advice_out[pool_id] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.exception("pool=%s 建议失败", pool_id)
                exit_code = 1
        report["advice"] = {
            "status": "ok" if any_ok else "failed",
            "pools": advice_out,
        }
        if not any_ok:
            logger.error("双池建议均未成功，仍尝试影子仓盯市（可能只 MTM、不开仓）")
            exit_code = 1

    # --- 3) 影子仓 ---
    if args.skip_shadow:
        report["shadow"] = {"status": "skipped"}
        logger.info("跳过影子仓（--skip-shadow）")
    else:
        from advice.shadow_farm import run_shadow_farm_once

        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else None
        try:
            shadow = run_shadow_farm_once(as_of=as_of, open_new_cohort=True)
            status = shadow.get("status")
            report["shadow"] = {
                "status": status,
                "as_of": shadow.get("as_of"),
                "n_strategies": len(shadow.get("strategies") or []),
            }
            if status not in ("ok", "skipped_data"):
                exit_code = 1
        except Exception as exc:
            report["shadow"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.exception("影子仓失败")
            exit_code = 1

    report["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    report["exit_code"] = exit_code
    _emit_report(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
