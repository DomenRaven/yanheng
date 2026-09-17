"""有界队列 + 专用写线程：fetch 与 DuckDB 写入重叠。

见 docs/ingestion-engine-research.md ADR。
"""
from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, TypeVar

from tqdm import tqdm

from common.http_retry import IntervalLimiter, default_limiter
from common.ingestion_engine.config import write_queue_depth

logger = logging.getLogger("ingestion_engine.bounded")

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class _WriteJob(Generic[T, R]):
    item: T
    result: R | None
    error: BaseException | None


def bounded_map_fetch_then_write(
    items: Iterable[T],
    fetch_fn: Callable[[T], R],
    on_result: Callable[[T, R | None, BaseException | None], None],
    *,
    workers: int,
    desc: str = "",
    limiter: IntervalLimiter | None = None,
    queue_depth: int | None = None,
) -> dict:
    """fetch 在线程池；on_result 仅在专用写线程执行（单消费者）。

    返回统计：writer_errors（写回调抛出的次数）。
    """
    job_list = list(items)
    stats = {"writer_errors": 0}
    if not job_list:
        return stats

    n_workers = max(1, workers)
    gate = default_limiter() if limiter is None else limiter
    depth = queue_depth if queue_depth is not None else write_queue_depth(n_workers)
    write_q: queue.Queue[_WriteJob[T, R] | None] = queue.Queue(maxsize=max(1, depth))
    stop = threading.Event()

    def writer_loop() -> None:
        while True:
            job = write_q.get()
            try:
                if job is None:
                    return
                try:
                    on_result(job.item, job.result, job.error)
                except BaseException as exc:  # noqa: BLE001
                    stats["writer_errors"] += 1
                    logger.exception("写线程 on_result 失败 item=%s: %s", job.item, exc)
            finally:
                write_q.task_done()

    writer = threading.Thread(target=writer_loop, name="ingestion-writer", daemon=False)
    writer.start()

    def _run(item: T) -> R:
        gate.wait()
        return fetch_fn(item)

    def _enqueue(item: T, result: R | None, err: BaseException | None) -> None:
        if stop.is_set():
            return
        write_q.put(_WriteJob(item=item, result=result, error=err))

    try:
        if n_workers <= 1:
            for item in tqdm(job_list, desc=desc):
                try:
                    result = _run(item)
                except BaseException as exc:  # noqa: BLE001
                    _enqueue(item, None, exc)
                    continue
                _enqueue(item, result, None)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_run, item): item for item in job_list}
                for fut in tqdm(as_completed(futures), total=len(job_list), desc=desc):
                    item = futures[fut]
                    try:
                        result = fut.result()
                    except BaseException as exc:  # noqa: BLE001
                        _enqueue(item, None, exc)
                        continue
                    _enqueue(item, result, None)
    finally:
        write_q.put(None)
        writer.join()

    return stats
