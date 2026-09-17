"""数据更新引擎配置（config.yaml -> ingestion.pipeline）。"""
from __future__ import annotations

from common.config import get_config


def pipeline_async_write() -> bool:
    return bool(get_config().get("ingestion", {}).get("pipeline", {}).get("async_write", True))


def write_queue_depth(http_workers: int) -> int:
    pipe = get_config().get("ingestion", {}).get("pipeline", {})
    depth = pipe.get("write_queue_depth")
    if depth is not None:
        return max(1, int(depth))
    return max(2, http_workers * 2)


def write_batch_size() -> int:
    return max(1, int(get_config().get("ingestion", {}).get("pipeline", {}).get("write_batch_size", 1)))
