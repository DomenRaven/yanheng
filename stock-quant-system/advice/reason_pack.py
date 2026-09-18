"""Top-N 结构化理由包（规格 S1）。

理论：
- `docs/07-产品设计启示/02-场景化建议引擎.md`：reasons[] 带 type/ref/detail；冲突不偷偷平均。
- `docs/04-风险管理/03-场景化决策建议框架.md`：信号冲突要展示，而不是平均掉。
- `docs/02-经典理论/05-交易心理学专论.md`：确认偏误 → 强制列出反方（行为冲突）。
- `advice/scanner.py` 原设计：行为信号只做交叉验证，**不改 pred_score / rank**。

本模块只编排已有列，不重新打分。
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def _fmt(val: Any, digits: int = 4) -> str:
    try:
        return f"{float(val):.{digits}f}"
    except (TypeError, ValueError):
        return str(val)


def build_reason_pack(row: pd.Series) -> dict[str, Any]:
    """从扫描行构造理由包：因子 + 行为冲突 + 一句话。"""
    factors: list[dict[str, str]] = []
    if pd.notna(row.get("pred_score")):
        rank = row.get("rank")
        detail = f"模型打分 {_fmt(row['pred_score'])}"
        if pd.notna(rank):
            detail += f"，全市场排名第{int(rank)}"
        factors.append({"type": "model", "ref": "champion_model", "detail": detail})
    if pd.notna(row.get("factor_mom_12_1")):
        factors.append({
            "type": "factor",
            "ref": "MOM-12-1",
            "detail": f"12月动量(剔近1月): {_fmt(row['factor_mom_12_1'])}",
        })
    if pd.notna(row.get("factor_roe")):
        factors.append({
            "type": "factor",
            "ref": "ROE",
            "detail": f"ROE: {_fmt(row['factor_roe'])}",
        })
    if pd.notna(row.get("factor_bp")):
        factors.append({
            "type": "factor",
            "ref": "BP",
            "detail": f"账面市值比: {_fmt(row['factor_bp'])}",
        })

    conflicts: list[dict[str, str]] = []
    flag = row.get("conflict_flag")
    if pd.notna(flag) and str(flag).strip() and str(flag) != "无冲突":
        conflicts.append({
            "type": "behavior",
            "ref": "conflict_flag",
            "detail": f"行为金融冲突（反方，不改排名）: {flag}",
        })

    rank_bit = f"排名第{int(row['rank'])}" if pd.notna(row.get("rank")) else "未排名"
    if conflicts:
        contra = "；".join(c["detail"].split(": ", 1)[-1] for c in conflicts)
        one_liner = f"{rank_bit}。多头看因子/模型，但存在冲突：{contra}。冲突只提示、不改排序。"
    else:
        one_liner = f"{rank_bit}。因子与行为代理暂无冲突提示；排序仍只来自冠军模型。"

    return {
        "factors": factors,
        "conflicts": conflicts,
        "one_liner": one_liner,
        "reasons": factors + conflicts,
        "has_conflict": bool(conflicts),
    }


def attach_reason_packs(df: pd.DataFrame) -> pd.DataFrame:
    """给扫描表增加一句话与冲突摘要列；保证不改 rank / pred_score。"""
    if df.empty:
        return df
    out = df.copy()
    ranks_before = out["rank"].tolist() if "rank" in out.columns else None
    scores_before = out["pred_score"].tolist() if "pred_score" in out.columns else None
    packs = [build_reason_pack(row) for _, row in out.iterrows()]
    out["reason_one_liner"] = [p["one_liner"] for p in packs]
    out["reason_has_conflict"] = [p["has_conflict"] for p in packs]
    if ranks_before is not None and out["rank"].tolist() != ranks_before:
        raise RuntimeError("理由包改变了 rank，违反 S1/scanner 不改排序约定")
    if scores_before is not None and out["pred_score"].tolist() != scores_before:
        raise RuntimeError("理由包改变了 pred_score，违反 S1/scanner 不改排序约定")
    return out
