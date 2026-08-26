"""时序验证切分：Purged K-Fold + Embargo，以及 Walk-Forward。

不训练模型，只产生 (train_idx, test_idx)。标签区间用 [i, i+label_horizon]
近似"未来收益窗口"，与理论库 `docs/03-量化方法/10-时序验证与标签工程.md` 对齐。
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np


def purged_kfold_splits(
    n_samples: int,
    n_splits: int = 5,
    label_horizon: int = 10,
    embargo: int = 5,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    if n_splits < 2:
        raise ValueError("n_splits 至少为 2")
    if n_samples < n_splits * 2:
        raise ValueError("样本量不足以做该折数的时序切分")

    fold_sizes = np.full(n_splits, n_samples // n_splits, dtype=int)
    fold_sizes[: n_samples % n_splits] += 1
    bounds = np.cumsum(np.concatenate([[0], fold_sizes]))

    for k in range(n_splits):
        test_start, test_end = int(bounds[k]), int(bounds[k + 1])
        test_idx = np.arange(test_start, test_end)
        purge_lo = max(0, test_start - label_horizon)
        embargo_hi = min(n_samples, test_end + embargo)
        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[purge_lo:embargo_hi] = False
        train_idx = np.where(train_mask)[0]
        yield train_idx, test_idx


def walk_forward_splits(
    n_samples: int,
    train_size: int,
    test_size: int,
    step: int | None = None,
    expanding: bool = False,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """滚动前推。expanding=True 时训练集从起点一直扩到测试窗之前（anchored）。"""
    step = step or test_size
    test_start = train_size
    while test_start + test_size <= n_samples:
        train_lo = 0 if expanding else test_start - train_size
        train_idx = np.arange(train_lo, test_start)
        test_idx = np.arange(test_start, test_start + test_size)
        yield train_idx, test_idx
        test_start += step
