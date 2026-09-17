"""数据更新引擎沙箱验收（不碰 data/warehouse.duckdb）。闸门 4。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.http_retry import IntervalLimiter
from common.ingestion_engine.bounded_pipeline import bounded_map_fetch_then_write
from common.parallel_fetch import map_fetch_then_write


def test_writer_runs_on_dedicated_thread() -> None:
    main = threading.get_ident()
    writer_ids: list[int] = []
    gate = IntervalLimiter(0.0, 0.0)

    def fetch(x: int) -> int:
        time.sleep(0.02)
        return x

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        assert err is None
        writer_ids.append(threading.get_ident())
        time.sleep(0.01)

    stats = bounded_map_fetch_then_write(
        range(6), fetch, on_result, workers=2, limiter=gate, queue_depth=2, desc="writer-thread"
    )
    assert stats["writer_errors"] == 0
    assert writer_ids
    assert all(wid != main for wid in writer_ids)


def test_backpressure_limits_queue() -> None:
    """写故意变慢时，在途任务数不应超过 queue_depth + workers 量级。"""
    gate = IntervalLimiter(0.0, 0.0)
    in_flight = {"n": 0}
    max_seen = {"m": 0}
    lock = threading.Lock()
    depth = 2

    def fetch(x: int) -> int:
        with lock:
            in_flight["n"] += 1
            max_seen["m"] = max(max_seen["m"], in_flight["n"])
        time.sleep(0.05)
        with lock:
            in_flight["n"] -= 1
        return x

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        time.sleep(0.08)

    bounded_map_fetch_then_write(
        range(12), fetch, on_result, workers=4, limiter=gate, queue_depth=depth, desc="backpressure"
    )
    assert max_seen["m"] <= depth + 4 + 1


def test_fetch_write_overlap_while_writer_busy() -> None:
    """单 fetch 工人时，下一次 fetch 应能与上一次写库重叠。"""
    gate = IntervalLimiter(0.0, 0.0)
    writer_busy = threading.Event()
    saw_overlap = threading.Event()
    lock = threading.Lock()

    def fetch(x: int) -> int:
        if writer_busy.is_set():
            saw_overlap.set()
        time.sleep(0.05)
        return x

    def on_write(_item: int, _result: int | None, err: BaseException | None) -> None:
        assert err is None
        writer_busy.set()
        time.sleep(0.08)
        writer_busy.clear()

    bounded_map_fetch_then_write(
        range(8), fetch, on_write, workers=1, limiter=gate, queue_depth=4, desc="overlap"
    )
    assert saw_overlap.is_set()


def test_legacy_map_fetch_on_caller_thread_when_async_disabled() -> None:
    """async_write=false 时保持 parallel_fetch 旧契约（写回调在调用线程）。"""
    import common.config as cfg_mod

    c = cfg_mod.get_config()
    pipe = c.setdefault("ingestion", {}).setdefault("pipeline", {})
    prev = pipe.get("async_write", True)
    pipe["async_write"] = False
    try:
        main = threading.get_ident()
        seen: list[int] = []
        gate = IntervalLimiter(0.0, 0.0)

        def fetch(x: int) -> int:
            return x

        def on_result(item: int, result: int | None, err: BaseException | None) -> None:
            seen.append(threading.get_ident())

        map_fetch_then_write(range(4), fetch, on_result, workers=1, limiter=gate)
        assert all(tid == main for tid in seen)
    finally:
        pipe["async_write"] = prev


def test_writer_error_counted() -> None:
    gate = IntervalLimiter(0.0, 0.0)

    def fetch(x: int) -> int:
        return x

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        if item == 2:
            raise RuntimeError("write boom")

    stats = bounded_map_fetch_then_write(
        range(4), fetch, on_result, workers=2, limiter=gate, queue_depth=2
    )
    assert stats["writer_errors"] == 1


def test_pipeline_state_resume_skip() -> None:
    import tempfile
    from pathlib import Path

    from common.ingestion_engine.pipeline_state import (
        mark_step,
        new_run,
        save_state,
        steps_to_skip_on_resume,
    )

    with tempfile.TemporaryDirectory() as tmp:
        import common.ingestion_engine.pipeline_state as ps

        fake_db = Path(tmp) / "warehouse.duckdb"
        fake_db.parent.mkdir(parents=True, exist_ok=True)
        fake_db.write_bytes(b"")
        orig = ps.get_db_path
        ps.get_db_path = lambda: fake_db  # type: ignore[assignment]
        try:
            state = new_run({"only": None, "skip": None})
            mark_step(state, "universe", {"status": "ok", "elapsed_s": 1})
            mark_step(state, "quotes", {"status": "failed", "error": "x"})
            skip = steps_to_skip_on_resume(state, ["universe", "quotes", "market_data"])
            assert skip == {"universe"}
        finally:
            ps.get_db_path = orig  # type: ignore[assignment]


def test_substep_mark_clear_on_step_ok() -> None:
    from common.ingestion_engine import pipeline_state as ps
    from common.ingestion_engine.pipeline_state import (
        clear_substeps,
        mark_substep,
        mark_step,
        new_run,
        substep_is_ok,
    )

    with tempfile.TemporaryDirectory() as tmp:
        fake_db = Path(tmp) / "t.duckdb"
        orig = ps.get_db_path
        ps.get_db_path = lambda: fake_db  # type: ignore[assignment]
        try:
            state = new_run({"only": None, "skip": None})
            mark_substep(state, "tushare_market_data", "daily_basic", {"status": "ok", "stats": {"ok": 1}})
            assert substep_is_ok(state, "tushare_market_data", "daily_basic")
            assert not substep_is_ok(state, "tushare_market_data", "index_weight")
            mark_step(state, "tushare_market_data", {"status": "ok", "elapsed_s": 1})
            clear_substeps(state, "tushare_market_data")
            state = ps.load_state()
            assert state is not None
            assert not substep_is_ok(state, "tushare_market_data", "daily_basic")
        finally:
            ps.get_db_path = orig  # type: ignore[assignment]


def main() -> None:
    test_pipeline_state_resume_skip()
    test_substep_mark_clear_on_step_ok()
    test_writer_runs_on_dedicated_thread()
    test_backpressure_limits_queue()
    test_fetch_write_overlap_while_writer_busy()
    test_legacy_map_fetch_on_caller_thread_when_async_disabled()
    test_writer_error_counted()
    print("test_ingestion_engine: all passed")


if __name__ == "__main__":
    main()
