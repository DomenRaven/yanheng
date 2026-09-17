"""并发取数 + 异步写库编排（见 docs/ingestion-engine-requirements.md）。"""
from common.ingestion_engine.bounded_pipeline import bounded_map_fetch_then_write
from common.ingestion_engine.config import pipeline_async_write, write_batch_size, write_queue_depth

__all__ = [
    "bounded_map_fetch_then_write",
    "pipeline_async_write",
    "write_batch_size",
    "write_queue_depth",
]
