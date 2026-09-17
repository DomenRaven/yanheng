"""Phase 5 阶段 B：以损定仓与 open→watch 降级。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.position_sizing import compute_open_size, default_equity_cny


def test_10k_cannot_buy_expensive_lot():
    eq = default_equity_cny()
    assert eq == 10_000.0
    # ¥500 股，8% 止损 → 每股风险 40；1 手名义 5 万远超 1 万账户
    sz = compute_open_size(eq, price=500.0, stop_price=460.0, exchange="sh")
    assert sz.shares == 0, sz
    print("OK: 1万账户买不起 ¥500/股 1 手 → shares=0", sz.reason)


def test_10k_can_buy_cheap_lot():
    eq = 10_000.0
    sz = compute_open_size(eq, price=8.0, stop_price=7.2, exchange="sz")
    assert sz.shares >= 100
    assert sz.est_amount_cny <= eq
    assert sz.max_loss_cny > 0
    print(f"OK: 可买 {sz.shares} 股 约 ¥{sz.est_amount_cny} 最大亏 ¥{sz.max_loss_cny}")


if __name__ == "__main__":
    test_10k_cannot_buy_expensive_lot()
    test_10k_can_buy_cheap_lot()
    print("test_advice_sizing: all passed")
