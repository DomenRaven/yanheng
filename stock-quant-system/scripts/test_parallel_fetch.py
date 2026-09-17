"""HTTP 线程池：写回调在调用线程；限速是全局发车间隔。不碰主库。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import common.config as _cfg_mod
from common.http_retry import IntervalLimiter
from common.parallel_fetch import map_fetch_then_write


def _force_sync_write_mode() -> None:
    """parallel_fetch 旧契约测试：写回调在调用线程。"""
    c = _cfg_mod.get_config()
    c.setdefault("ingestion", {}).setdefault("pipeline", {})["async_write"] = False


def test_limiter_global_interval() -> None:
    gate = IntervalLimiter(0.05, 0.05)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(4):
            gate.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ordered = sorted(stamps)
    assert len(ordered) == 16
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    too_close = sum(1 for g in gaps if g < 0.02)
    assert too_close <= 1, gaps


def test_on_result_runs_on_caller_thread() -> None:
    _force_sync_write_mode()
    main = threading.get_ident()
    seen: list[int] = []
    gate = IntervalLimiter(0.0, 0.0)

    def fetch(x: int) -> int:
        time.sleep(0.03)
        return x

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        assert threading.get_ident() == main
        assert err is None
        seen.append(result if result is not None else -1)

    map_fetch_then_write(range(8), fetch, on_result, workers=4, limiter=gate, desc="pool")
    assert sorted(seen) == list(range(8))


def test_workers_one_stays_on_caller_thread() -> None:
    _force_sync_write_mode()
    main = threading.get_ident()
    seen: list[int] = []
    gate = IntervalLimiter(0.0, 0.0)

    def fetch(x: int) -> int:
        return x * 2

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        assert threading.get_ident() == main
        assert err is None
        seen.append(result if result is not None else -1)

    map_fetch_then_write(range(5), fetch, on_result, workers=1, limiter=gate, desc="serial")
    assert seen == [0, 2, 4, 6, 8]


def test_fetch_error_reaches_on_result_once() -> None:
    calls: list[tuple[int, bool]] = []
    gate = IntervalLimiter(0.0, 0.0)

    def fetch(x: int) -> int:
        if x == 3:
            raise RuntimeError("boom")
        return x

    def on_result(item: int, result: int | None, err: BaseException | None) -> None:
        calls.append((item, err is not None))

    map_fetch_then_write(range(5), fetch, on_result, workers=3, limiter=gate)
    assert len(calls) == 5
    by_item = dict(calls)
    assert by_item[3] is True
    assert sum(1 for _, failed in calls if failed) == 1


def test_pool_faster_than_serial_when_latency_dominates() -> None:
    gate = IntervalLimiter(0.02, 0.02)

    def fetch(_x: int) -> int:
        time.sleep(0.12)
        return _x

    def on_result(_item: int, _result: int | None, _err: BaseException | None) -> None:
        return

    t0 = time.monotonic()
    map_fetch_then_write(range(8), fetch, on_result, workers=4, limiter=gate)
    elapsed = time.monotonic() - t0
    # 串行约 8*(0.12+0.02)=1.12s；4 工人应明显低于 1.0s
    assert elapsed < 1.0, elapsed


def test_pipeline_parallel_wave_overlaps_three_steps() -> None:
    """fundamentals / income / corporate_actions 必须同时开工（公司行为在步骤表里并不相邻）。"""
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "orchestrator_under_test", _ROOT / "common" / "ingestion_engine" / "orchestrator.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    barrier = threading.Barrier(3)
    finished: list[str] = []
    lock = threading.Lock()

    def make(name: str, join: bool):
        def _fn(_args: argparse.Namespace) -> dict:
            if join:
                barrier.wait(timeout=2.0)
            with lock:
                finished.append(name)
            return {}

        return _fn

    mod.STEP_FUNCS = {
        "universe": make("universe", False),
        "quotes": make("quotes", False),
        "fundamentals": make("fundamentals", True),
        "income_statement": make("income_statement", True),
        "reference_data": make("reference_data", False),
        "market_data": make("market_data", False),
        "corporate_actions": make("corporate_actions", True),
        "tushare_prices": make("tushare_prices", False),
        "tushare_market_data": make("tushare_market_data", False),
        "tushare_behavior": make("tushare_behavior", False),
    }
    args = argparse.Namespace(
        only=None,
        skip=None,
        quotes_limit=None,
        fundamentals_limit=None,
        corporate_actions_limit=None,
        tushare_limit=None,
    )
    results = mod.run_data_update(args)
    step_results = {k: v for k, v in results.items() if k != "_meta"}
    assert all(r["status"] == "ok" for r in step_results.values()), results
    assert set(finished[:2]) == {"universe", "quotes"}
    assert set(finished[2:5]) == {"fundamentals", "income_statement", "corporate_actions"}
    assert finished[5:] == ["reference_data", "market_data", "tushare_prices", "tushare_market_data", "tushare_behavior"]


if __name__ == "__main__":
    test_limiter_global_interval()
    test_on_result_runs_on_caller_thread()
    test_workers_one_stays_on_caller_thread()
    test_fetch_error_reaches_on_result_once()
    test_pool_faster_than_serial_when_latency_dominates()
    test_pipeline_parallel_wave_overlaps_three_steps()
    print("test_parallel_fetch: ok")
