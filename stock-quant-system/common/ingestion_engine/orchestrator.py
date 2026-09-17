"""数据更新步骤编排（与 daily_pipeline 步骤集一致）。"""
from __future__ import annotations

import argparse
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from common.ingestion_engine.pipeline_state import (
    args_compatible,
    clear_substeps,
    load_state,
    mark_run_finished,
    mark_step,
    new_run,
    save_state,
    state_file_path,
    steps_to_skip_on_resume,
)

logger = logging.getLogger("ingestion_engine.orchestrator")

STEP_ORDER = [
    "universe",
    "quotes",
    "fundamentals",
    "income_statement",
    "reference_data",
    "market_data",
    "corporate_actions",
    "tushare_prices",
    "tushare_market_data",
    "tushare_behavior",
]

PARALLEL_STOCK_STEPS = ("fundamentals", "income_statement", "corporate_actions")

_RUN_STATE: dict[str, Any] | None = None


def _args_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "only": args.only,
        "skip": args.skip,
        "quotes_limit": args.quotes_limit,
        "fundamentals_limit": args.fundamentals_limit,
        "corporate_actions_limit": args.corporate_actions_limit,
        "tushare_limit": args.tushare_limit,
    }


def _install_interrupt_handler() -> None:
    def _on_signal(signum: int, _frame: object) -> None:
        if _RUN_STATE is not None:
            mark_run_finished(_RUN_STATE, status="interrupted")
            logger.warning("收到中断信号 %s，已写入断点状态: %s", signum, state_file_path())
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass


def _run_universe() -> dict:
    from ingestion.universe import sync_universe

    df = sync_universe()
    return {"rows": len(df)}


def _run_quotes(limit: int | None) -> dict:
    from ingestion.quotes_batch import sync_quotes_batch

    return sync_quotes_batch(limit=limit)


def _run_fundamentals(limit: int | None) -> dict:
    from ingestion.fundamentals_batch import sync_fundamentals_batch, sync_fundamentals_bse_tushare

    sina_stats = sync_fundamentals_batch(limit=limit)
    bse_stats = sync_fundamentals_bse_tushare(limit=limit)
    return {"sina": sina_stats, "bse_tushare": bse_stats}


def _run_income_statement(limit: int | None) -> dict:
    from ingestion.income_statement_batch import sync_income_statement_batch

    return sync_income_statement_batch(limit=limit, source="tushare")


def _run_reference_data() -> dict:
    from ingestion.reference_data import sync_all_reference_data

    return sync_all_reference_data()


def _run_market_data() -> dict:
    from ingestion.market_data import sync_all_market_data

    return sync_all_market_data()


def _run_corporate_actions(limit: int | None) -> dict:
    from ingestion.corporate_actions import sync_corporate_actions

    return sync_corporate_actions(limit=limit)


def _run_tushare_prices(limit: int | None) -> dict:
    from ingestion.tushare_prices import sync_all_tushare_prices

    return sync_all_tushare_prices(limit=limit)


def _run_tushare_market_data(limit: int | None) -> dict:
    from ingestion.tushare_market_data import sync_all_tushare_market_data

    return sync_all_tushare_market_data(limit=limit)


def _run_tushare_behavior(limit: int | None) -> dict:
    from ingestion.tushare_behavior import sync_all_tushare_behavior

    return sync_all_tushare_behavior(limit=limit)


STEP_FUNCS = {
    "universe": lambda args: _run_universe(),
    "quotes": lambda args: _run_quotes(args.quotes_limit),
    "fundamentals": lambda args: _run_fundamentals(args.fundamentals_limit),
    "income_statement": lambda args: _run_income_statement(args.fundamentals_limit),
    "reference_data": lambda args: _run_reference_data(),
    "market_data": lambda args: _run_market_data(),
    "corporate_actions": lambda args: _run_corporate_actions(args.corporate_actions_limit),
    "tushare_prices": lambda args: _run_tushare_prices(args.tushare_limit),
    "tushare_market_data": lambda args: _run_tushare_market_data(args.tushare_limit),
    "tushare_behavior": lambda args: _run_tushare_behavior(args.tushare_limit),
}


def _run_one_step(step: str, args: argparse.Namespace) -> tuple[str, dict]:
    t_step = time.time()
    try:
        stats = STEP_FUNCS[step](args)
        return step, {"status": "ok", "stats": stats, "elapsed_s": round(time.time() - t_step, 1)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s 失败，跳过，下次运行会重试", step)
        return step, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _init_pipeline_state(args: argparse.Namespace) -> dict[str, Any]:
    global _RUN_STATE
    snap = _args_snapshot(args)
    if getattr(args, "fresh_run", False):
        state = new_run(snap)
        save_state(state)
        _RUN_STATE = state
        logger.info("新流水线 run_id=%s，状态文件 %s", state["run_id"], state_file_path())
        return state

    if getattr(args, "resume", False):
        prev = load_state()
        if prev is None:
            logger.warning("无断点文件，从头开始")
            state = new_run(snap)
        elif not args_compatible(prev, snap):
            logger.error("断点参数与当前命令不一致（only/skip/limit），请用相同参数 --resume 或 --fresh-run")
            state = new_run(snap)
        else:
            state = prev
            state["status"] = "running"
            save_state(state)
            logger.info("续跑 run_id=%s，已完成步骤: %s", state["run_id"], list(state.get("steps", {}).keys()))
        _RUN_STATE = state
        return state

    state = new_run(snap)
    save_state(state)
    _RUN_STATE = state
    return state


def _persist_step(state: dict[str, Any], step: str, rec: dict) -> None:
    mark_step(
        state,
        step,
        {
            "status": rec.get("status"),
            "elapsed_s": rec.get("elapsed_s"),
            "error": rec.get("error"),
        },
    )
    if rec.get("status") == "ok":
        clear_substeps(state, step)


def run_data_update(args: argparse.Namespace) -> dict:
    global _RUN_STATE
    _install_interrupt_handler()
    state = _init_pipeline_state(args)
    _RUN_STATE = state

    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()

    remaining = [s for s in STEP_ORDER if (only is None or s in only) and s not in skip]
    resume_skip = steps_to_skip_on_resume(state, remaining) if getattr(args, "resume", False) else set()

    results: dict[str, dict] = {}
    for step in resume_skip:
        prev = state["steps"][step]
        results[step] = {
            "status": "ok",
            "skipped_resume": True,
            "elapsed_s": prev.get("elapsed_s"),
        }
        logger.info("=== 跳过 %s（断点已完成）===", step)

    remaining = [s for s in remaining if s not in resume_skip]
    t0 = time.time()

    def record(step: str, rec: dict) -> None:
        results[step] = rec
        _persist_step(state, step, rec)
        logger.info("%s 完成，耗时 %s: %s", step, rec.get("elapsed_s"), rec.get("status") or rec)

    try:
        while remaining:
            if remaining[0] in PARALLEL_STOCK_STEPS:
                bundle = [s for s in PARALLEL_STOCK_STEPS if s in remaining]
                for s in bundle:
                    remaining.remove(s)
                if len(bundle) == 1:
                    step, rec = _run_one_step(bundle[0], args)
                    record(step, rec)
                    continue
                logger.info("=== 并行 HTTP: %s ===", " || ".join(bundle))
                with ThreadPoolExecutor(max_workers=len(bundle)) as pool:
                    futs = [pool.submit(_run_one_step, s, args) for s in bundle]
                    for fut in as_completed(futs):
                        step, rec = fut.result()
                        record(step, rec)
                continue

            step = remaining.pop(0)
            logger.info("=== Step %s ===", step)
            name, rec = _run_one_step(step, args)
            record(name, rec)
    finally:
        ok = sum(1 for r in results.values() if r.get("status") == "ok")
        if ok == len(results) and results:
            mark_run_finished(state, status="completed")
        elif state.get("status") == "running":
            save_state(state)

    ok = sum(1 for r in results.values() if r.get("status") == "ok")
    logger.info(
        "run_data_update 结束，耗时 %.1f 分钟，%d/%d 步骤成功；run_id=%s",
        (time.time() - t0) / 60,
        ok,
        len(results),
        state["run_id"],
    )
    results["_meta"] = {
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "steps_ok": ok,
        "steps_total": len(results),
        "run_id": state["run_id"],
        "state_file": str(state_file_path()),
    }
    return results


def build_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--only", type=str, default=None, help=f"逗号分隔。可选：{','.join(STEP_ORDER)}")
    parser.add_argument("--skip", type=str, default=None, help="逗号分隔，跳过指定步骤")
    parser.add_argument("--quotes-limit", type=int, default=None)
    parser.add_argument("--fundamentals-limit", type=int, default=None)
    parser.add_argument("--corporate-actions-limit", type=int, default=None)
    parser.add_argument(
        "--tushare-limit",
        type=int,
        default=None,
        help="调试用：Tushare 三步内逐股/逐日任务的条数上限",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从 data/ingestion_pipeline_state.json 续跑：跳过本 run 已成功的整步；步内靠库内增量+upsert",
    )
    parser.add_argument(
        "--fresh-run",
        action="store_true",
        help="忽略旧断点，开始新的 run_id（全量更新请用此选项）",
    )
    return parser
