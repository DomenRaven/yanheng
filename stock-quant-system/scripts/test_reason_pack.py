"""S1：结构化理由包（不碰仓库、不改排名）。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pandas as pd

from advice.reason_pack import attach_reason_packs, build_reason_pack


def main() -> None:
    bull = pd.Series({
        "symbol": "000001",
        "rank": 3,
        "pred_score": 0.12,
        "factor_roe": 0.15,
        "factor_mom_12_1": 0.08,
        "factor_bp": 0.4,
        "conflict_flag": "无冲突",
    })
    pack_ok = build_reason_pack(bull)
    assert pack_ok["has_conflict"] is False
    types = {r["type"] for r in pack_ok["reasons"]}
    assert "model" in types and "factor" in types
    assert "behavior" not in types
    assert "排名第3" in pack_ok["one_liner"]
    assert any(r["ref"] == "ROE" for r in pack_ok["factors"])

    bear = pd.Series({
        "symbol": "000002",
        "rank": 1,
        "pred_score": 0.99,
        "factor_roe": 0.2,
        "factor_mom_12_1": 0.3,
        "conflict_flag": "浮盈筹码集中；游资高频出没",
    })
    pack_cf = build_reason_pack(bear)
    assert pack_cf["has_conflict"] is True
    assert any(r["type"] == "behavior" for r in pack_cf["reasons"])
    assert "冲突" in pack_cf["one_liner"]
    # 确认偏误护栏：反方出现在 reasons，而不是只有多头因子
    assert pack_cf["reasons"][-1]["type"] == "behavior"

    df = pd.DataFrame([bull, bear])
    ranked = df.sort_values("rank").reset_index(drop=True)
    attached = attach_reason_packs(ranked)
    assert attached["rank"].tolist() == ranked["rank"].tolist()
    assert attached["pred_score"].tolist() == ranked["pred_score"].tolist()
    assert attached.loc[attached["symbol"] == "000002", "reason_has_conflict"].iloc[0]
    print("OK S1 reason pack", pack_cf["one_liner"][:40])
    print("test_reason_pack: passed")


if __name__ == "__main__":
    main()
