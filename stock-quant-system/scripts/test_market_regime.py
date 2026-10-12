"""MR-A：市场档位合成序列 + 卡片只标注不改仓。不连接生产库。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pandas as pd

from advice.market_regime import annotate_cards, classify_closes, load_market_regime


def _rising(n: int = 40, step: float = 1.004) -> list[float]:
    px = 100.0
    out = []
    for _ in range(n):
        px *= step
        out.append(px)
    return out


def _falling(n: int = 40, step: float = 0.997) -> list[float]:
    px = 100.0
    out = []
    for _ in range(n):
        px *= step
        out.append(px)
    return out


def test_classify_bands() -> None:
    up = classify_closes(_rising())
    assert up["regime"] == "offense", up
    assert up["above_ma"] is True
    assert up["ret_n"] >= 0.03

    down = classify_closes(_falling())
    assert down["regime"] == "defense", down
    assert down["above_ma"] is False
    assert down["ret_n"] <= -0.03

    flat = classify_closes([100.0] * 30)
    assert flat["regime"] == "neutral", flat

    short = classify_closes([100.0] * 10)
    assert short["regime"] == "unknown", short

    # 刚好 +3% 且在均线上
    exact_up = [100.0] * 20 + [103.0]
    assert classify_closes(exact_up)["regime"] == "offense"
    # 刚好 -3% 且在均线下
    exact_down = [100.0] * 20 + [97.0]
    got = classify_closes(exact_down)
    assert got["regime"] == "defense", got
    # 在均线上但近20日只有 +1% → 中性，不能算进攻
    mild = [100.0] * 20 + [101.0]
    assert classify_closes(mild)["regime"] == "neutral"


def test_annotate_does_not_change_size() -> None:
    regime = {
        "regime": "defense",
        "benchmark": "sh000300",
        "card_note": "【市场】测试句。",
        "reason_detail": "沪深300防守",
        "risk_line": "大盘处于防守档：测试",
    }
    cards = [{
        "symbol": "000001",
        "action": "open",
        "size_shares": 200,
        "plain_summary": "建议买入。",
        "reasons": [{"type": "model", "detail": "排名第1"}],
        "risks": [],
    }]
    out = annotate_cards(cards, regime)
    assert out[0]["action"] == "open"
    assert out[0]["size_shares"] == 200
    assert any(r["type"] == "market" for r in out[0]["reasons"])
    assert "测试" in out[0]["risks"][0]
    assert "【市场】" in out[0]["plain_summary"]
    again = annotate_cards(out, regime)
    assert sum(1 for r in again[0]["reasons"] if r["type"] == "market") == 1


class _FakeConn:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self._code = ""

    def execute(self, _sql, params):
        self._code = params[0]
        return self

    def df(self) -> pd.DataFrame:
        return self.frames[self._code]


def _desc(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    df = pd.DataFrame({"trade_date": dates, "close": closes})
    return df.iloc[::-1].reset_index(drop=True)


def test_confirm_index_cannot_flip_benchmark() -> None:
    frames = {
        "sh000300": _desc(_rising()),
        "sh000001": _desc(_falling()),
        "sh000852": _desc(_falling()),
    }
    regime = load_market_regime(_FakeConn(frames))
    assert regime["regime"] == "offense", regime
    assert regime["confirm_agrees"] is False
    assert regime["style_spread"] is not None and regime["style_spread"] > 0


class _Boom:
    def execute(self, *_a, **_k):
        raise RuntimeError("locked")


def test_read_failure_is_unknown() -> None:
    regime = load_market_regime(_Boom())
    assert regime["regime"] == "unknown"
    assert regime["error"]


def main() -> None:
    test_classify_bands()
    test_annotate_does_not_change_size()
    test_confirm_index_cannot_flip_benchmark()
    test_read_failure_is_unknown()
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
