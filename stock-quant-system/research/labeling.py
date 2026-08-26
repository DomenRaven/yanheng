"""三重障碍标签（López de Prado）。用合成或真实价格序列均可，不依赖主库。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier(
    close: pd.Series,
    *,
    profit_take: float = 0.05,
    stop_loss: float = 0.05,
    max_holding: int = 10,
    vol_adjust: bool = True,
    vol_window: int = 20,
    vol_target: float = 0.02,
) -> pd.DataFrame:
    """对每个时点 t，观察 (t, t+max_holding] 内先触哪条障碍。

    返回列：
        label: 1 先触止盈，-1 先触止损，0 到期未触（按到期收益符号，为 0 则中性）
        touch_horizon: 触碰或到期的持有天数
        barrier: "up" / "down" / "time"
    """
    close = close.astype(float)
    n = len(close)
    labels = np.zeros(n, dtype=int)
    horizons = np.full(n, np.nan)
    barriers = np.array([""] * n, dtype=object)

    vol = close.pct_change().rolling(vol_window).std() if vol_adjust else None
    values = close.to_numpy()

    for i in range(n - 1):
        px = values[i]
        if not np.isfinite(px) or px <= 0:
            continue
        up_w, down_w = profit_take, stop_loss
        if vol_adjust:
            v = vol.iloc[i] if vol is not None else np.nan
            if np.isfinite(v) and v > 0:
                scale = v / vol_target
                up_w = profit_take * scale
                down_w = stop_loss * scale
        up_px = px * (1 + up_w)
        down_px = px * (1 - down_w)
        end = min(i + max_holding, n - 1)
        hit = None
        for j in range(i + 1, end + 1):
            p = values[j]
            if p >= up_px:
                hit = ("up", 1, j - i)
                break
            if p <= down_px:
                hit = ("down", -1, j - i)
                break
        if hit is None:
            ret = values[end] / px - 1
            lab = 1 if ret > 0 else (-1 if ret < 0 else 0)
            hit = ("time", lab, end - i)
        barriers[i], labels[i], horizons[i] = hit

    return pd.DataFrame(
        {"label": labels, "touch_horizon": horizons, "barrier": barriers},
        index=close.index,
    )
