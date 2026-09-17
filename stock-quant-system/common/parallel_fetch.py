"""HTTP 线程池 + 主线程写入。

ADR：不用 sidecar 双库（Phase 0.5 合并成本高），也不并行打开 DuckDB。
网络等待远大于限速间隔时，多工人同时 in-flight，发车仍由 IntervalLimiter 卡全局间隔。
Tushare Python 客户端非线程安全，调用方应对 Tushare 任务传 workers=1。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

from tqdm import tqdm

from common.config import get_config
from common.http_retry import IntervalLimiter, default_limiter
from common.ingestion_engine.config import pipeline_async_write

T = TypeVar("T")
R = TypeVar("R")


def http_workers() -> int:
    return int(get_config().get("ingestion", {}).get("http_workers", 6))


def map_fetch_then_write(
    items: Iterable[T],
    fetch_fn: Callable[[T], R],
    on_result: Callable[[T, R | None, BaseException | None], None],
    *,
    workers: int | None = None,
    desc: str = "",
    limiter: IntervalLimiter | None = None,
) -> None:
    """fetch_fn 在工作线程跑（只准 HTTP/计算）；on_result 在调用线程跑（只准写库）。

    limiter 默认新浪/东财共用 default_limiter。Tushare 传 tushare_limiter() 且 workers=1。

    ingestion.pipeline.async_write=true 时走有界队列 + 专用写线程（见 ingestion_engine）。
    """
    job_list = list(items)
    if not job_list:
        return
    n_workers = http_workers() if workers is None else workers
    gate = default_limiter() if limiter is None else limiter

    if pipeline_async_write():
        from common.ingestion_engine.bounded_pipeline import bounded_map_fetch_then_write

        bounded_map_fetch_then_write(
            job_list,
            fetch_fn,
            on_result,
            workers=n_workers,
            desc=desc,
            limiter=gate,
        )
        return

    def _run(item: T) -> R:
        gate.wait()
        return fetch_fn(item)

    if n_workers <= 1:
        for item in tqdm(job_list, desc=desc):
            try:
                result = _run(item)
            except BaseException as exc:  # noqa: BLE001 - 单条失败交给 on_result 记日志
                on_result(item, None, exc)
                continue
            on_result(item, result, None)
        return

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_run, item): item for item in job_list}
        for fut in tqdm(as_completed(futures), total=len(job_list), desc=desc):
            item = futures[fut]
            try:
                result = fut.result()
            except BaseException as exc:  # noqa: BLE001
                on_result(item, None, exc)
                continue
            on_result(item, result, None)
