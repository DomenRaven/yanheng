"""
通用重试装饰器：用于包裹所有对外部数据源（AKShare / 直接HTTP请求）的调用。

设计动机：实测环境中新浪财经接口(sina)整体稳定，但东方财富(eastmoney)部分接口
在本机代理/VPN环境下会出现间歇性 502 / ProxyError / RemoteDisconnected，
且部分故障是"持续若干分钟"而非单次瞬时抖动，因此重试策略采用：
  - 指数退避 + 随机抖动（避免多进程/多股票请求同时重试造成"重试风暴"）
  - 较高的默认重试次数（5次）与较长的最大等待（60秒），换取"脚本挂着跑，最终能拿到数据"
  - 所有失败最终仍失败时，抛出原始异常并由上层记录到失败队列，而不是静默丢弃数据
"""
from __future__ import annotations

import functools
import logging
import random
import threading
import time
from typing import Any, Callable, TypeVar

from common.config import get_config

logger = logging.getLogger("ingestion")

T = TypeVar("T")


class FetchFailedError(RuntimeError):
    """多次重试后仍失败时抛出，携带原始异常信息。"""

    def __init__(self, message: str, last_exception: Exception | None = None):
        super().__init__(message)
        self.last_exception = last_exception


def retry_on_failure(
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: float | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """装饰器：对被装饰函数应用指数退避重试。参数缺省时读取 config.yaml 的 ingestion.retry。"""

    cfg = get_config().get("ingestion", {}).get("retry", {})
    _max_attempts = max_attempts or cfg.get("max_attempts", 5)
    _base_delay = base_delay or cfg.get("base_delay_seconds", 2.0)
    _max_delay = max_delay or cfg.get("max_delay_seconds", 60.0)
    _jitter = jitter if jitter is not None else cfg.get("jitter_seconds", 1.0)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, _max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 数据源异常类型繁杂，统一捕获后分级处理
                    last_exc = exc
                    if attempt == _max_attempts:
                        break
                    delay = min(_base_delay * (2 ** (attempt - 1)), _max_delay)
                    delay += random.uniform(0, _jitter)
                    logger.warning(
                        "%s 第%d次调用失败(%s: %s)，%.1f秒后重试",
                        func.__name__, attempt, type(exc).__name__, str(exc)[:120], delay,
                    )
                    time.sleep(delay)
            raise FetchFailedError(
                f"{func.__name__} 在 {_max_attempts} 次重试后仍然失败", last_exc
            ) from last_exc

        return wrapper

    return decorator


class IntervalLimiter:
    """进程内共享的最小间隔。多线程并发请求时，发车间隔仍不低于配置，不是每线程各睡一遍。"""

    def __init__(self, min_interval: float, max_interval: float) -> None:
        if min_interval < 0 or max_interval < min_interval:
            raise ValueError("invalid limiter interval")
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._lock = threading.Lock()
        self._next_mono = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_mono)
            gap = random.uniform(self.min_interval, self.max_interval)
            self._next_mono = slot + gap
            sleep_for = slot - now
        if sleep_for > 0:
            time.sleep(sleep_for)


_DEFAULT_LIMITER: IntervalLimiter | None = None
_TUSHARE_LIMITER: IntervalLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def default_limiter() -> IntervalLimiter:
    global _DEFAULT_LIMITER
    with _LIMITER_LOCK:
        if _DEFAULT_LIMITER is None:
            cfg = get_config().get("ingestion", {}).get("rate_limit", {})
            _DEFAULT_LIMITER = IntervalLimiter(
                float(cfg.get("min_interval_seconds", 0.35)),
                float(cfg.get("max_interval_seconds", 0.8)),
            )
        return _DEFAULT_LIMITER


def tushare_limiter() -> IntervalLimiter:
    """2000 积分档约 200 次/分钟。间隔 0.35s ≈ 171 次/分钟，留余量。Tushare 客户端非线程安全，并发步仍用 workers=1。"""
    global _TUSHARE_LIMITER
    with _LIMITER_LOCK:
        if _TUSHARE_LIMITER is None:
            cfg = get_config().get("ingestion", {}).get("tushare_rate_limit", {})
            _TUSHARE_LIMITER = IntervalLimiter(
                float(cfg.get("min_interval_seconds", 0.35)),
                float(cfg.get("max_interval_seconds", 0.45)),
            )
        return _TUSHARE_LIMITER


def polite_sleep() -> None:
    """顺序循环用。并发抓取走 IntervalLimiter.wait，不要两个都调。"""
    default_limiter().wait()
