"""分池冠军指针（需求 R2）。

- `champion_hs.json` / `champion_bj.json`：各池独立晋升
- 迁移期：仅存在旧 `champion.json` 时，视为 **hs** 冠军；**禁止**用 hs 模型给 bj 打分
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

POOL_HS = "hs"
POOL_BJ = "bj"
VALID_POOLS = (POOL_HS, POOL_BJ)


def champion_file_for_pool(registry_dir: str | Path, pool_id: str) -> Path:
    registry_path = Path(registry_dir)
    if pool_id not in VALID_POOLS:
        raise ValueError(f"未知 pool_id={pool_id}，可选 {VALID_POOLS}")
    specific = registry_path / f"champion_{pool_id}.json"
    if specific.exists():
        return specific
    legacy = registry_path / "champion.json"
    if pool_id == POOL_HS and legacy.exists():
        return legacy
    raise FileNotFoundError(
        f"未找到池 {pool_id} 的冠军指针（期望 {specific.name}"
        + (" 或兼容 champion.json）" if pool_id == POOL_HS else "）")
        + "。禁止用另一池模型静默打分。"
    )


def load_champion_pointer(registry_dir: str | Path, pool_id: str) -> dict[str, Any]:
    path = champion_file_for_pool(registry_dir, pool_id)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    data["_champion_file"] = str(path)
    data["_pool_id"] = pool_id
    return data


def load_champion_model(registry_dir: str = "mlops/registry", *, pool_id: str = POOL_HS) -> tuple[object, dict]:
    import pickle

    registry_path = Path(registry_dir)
    champion = load_champion_pointer(registry_path, pool_id)
    run_dir = registry_path / champion["run_id"]
    if not run_dir.exists():
        raise FileNotFoundError(
            f"池 {pool_id} 冠军 run_id={champion['run_id']} 目录不存在（来自 {champion['_champion_file']}）"
        )
    with open(run_dir / "metadata.json", encoding="utf-8") as f:
        metadata = json.load(f)
    with open(run_dir / "model.pkl", "rb") as f:
        model = pickle.load(f)
    metadata = dict(metadata)
    metadata["pool_id"] = pool_id
    metadata["champion_file"] = champion["_champion_file"]
    return model, metadata


def filter_snapshot_by_pool(snapshot, pool_id: str):
    """按交易所过滤截面；CDR 689009 在 sh，归 hs。"""
    import pandas as pd

    if pool_id == POOL_HS:
        return snapshot[snapshot["exchange"].isin(["sh", "sz"])].copy()
    if pool_id == POOL_BJ:
        return snapshot[snapshot["exchange"] == "bj"].copy()
    raise ValueError(f"未知 pool_id={pool_id}")


def describe_champion(registry_dir: str | Path = "mlops/registry", *, pool_id: str) -> dict[str, Any]:
    """UI/说明书用：只读指针 + metadata，区分池、run、训练截面区间与样本外 RankIC。

    返回字段始终存在；缺文件时 run_id 为 None，ready=False。
    """
    registry_path = Path(registry_dir)
    label = "沪深池" if pool_id == POOL_HS else "北交所池"
    out: dict[str, Any] = {
        "pool_id": pool_id,
        "pool_label": label,
        "ready": False,
        "run_id": None,
        "promoted_at": None,
        "reason": None,
        "date_range": None,
        "walk_forward_rank_ic": None,
        "n_rows": None,
        "short": f"{label}：未就绪",
    }
    try:
        ptr = load_champion_pointer(registry_path, pool_id)
    except FileNotFoundError:
        return out
    run_id = ptr.get("run_id")
    out["run_id"] = run_id
    out["promoted_at"] = ptr.get("promoted_at")
    out["reason"] = ptr.get("reason")
    meta_path = registry_path / str(run_id) / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            out["date_range"] = meta.get("date_range")
            wf = meta.get("walk_forward_oos") or {}
            out["walk_forward_rank_ic"] = wf.get("rank_ic_mean")
            out["n_rows"] = meta.get("n_rows_total")
        except (json.JSONDecodeError, OSError):
            pass
    dr = out["date_range"]
    dr_s = f"{dr[0]}~{dr[1]}" if isinstance(dr, (list, tuple)) and len(dr) == 2 else "—"
    ic = out["walk_forward_rank_ic"]
    ic_s = f"{ic:.4f}" if isinstance(ic, (int, float)) else "—"
    out["ready"] = bool(run_id)
    out["short"] = f"{label} {run_id} · 面板{dr_s} · WF {ic_s}"
    return out
