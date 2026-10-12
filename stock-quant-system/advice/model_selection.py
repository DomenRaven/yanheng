"""沪深打分模型切换。不改冠军指针。

- 未选择时，扫描仍加载 `champion_hs.json` / `champion.json`。
- 只能切到 `model_catalog.json` 里登记过、且池为 hs 的 run。
- 北交所不参与切换，始终用 `champion_bj.json`。
- 禁止按文件夹新旧静默挑模型。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from advice.champion_registry import POOL_BJ, POOL_HS, describe_champion, load_champion_model

CHOICE_CHAMPION = "champion"
_CATALOG = "model_catalog.json"
_ACTIVE = "active_hs_model.json"


def _registry(registry_dir: str | Path) -> Path:
    return Path(registry_dir)


def catalog_models(registry_dir: str | Path = "mlops/registry") -> list[dict[str, Any]]:
    path = _registry(registry_dir) / _CATALOG
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    models = data.get("models") or []
    return [m for m in models if isinstance(m, dict)]


def register_selectable(
    registry_dir: str | Path,
    *,
    run_id: str,
    label: str,
    pool_id: str = POOL_HS,
    objective: str,
) -> None:
    if pool_id != POOL_HS:
        raise ValueError("目前只有沪深池可以登记可切换模型")
    registry = _registry(registry_dir)
    path = registry / _CATALOG
    models = catalog_models(registry)
    models = [m for m in models if m.get("run_id") != run_id]
    models.append(
        {
            "choice_id": run_id,
            "run_id": run_id,
            "label": label,
            "pool_id": pool_id,
            "objective": objective,
        }
    )
    path.write_text(json.dumps({"models": models}, ensure_ascii=False, indent=2), encoding="utf-8")


def _champion_run_id(registry_dir: str | Path) -> str | None:
    described = describe_champion(registry_dir, pool_id=POOL_HS)
    return described["run_id"] if described.get("ready") else None


def list_hs_choices(registry_dir: str | Path = "mlops/registry") -> list[dict[str, Any]]:
    """界面按钮用。第一项永远是现行冠军。"""
    registry = _registry(registry_dir)
    champ = describe_champion(registry, pool_id=POOL_HS)
    choices = [
        {
            "choice_id": CHOICE_CHAMPION,
            "label": "现行冠军",
            "run_id": champ.get("run_id"),
            "rank_ic": champ.get("walk_forward_rank_ic"),
            "ready": bool(champ.get("ready")),
            "is_champion_pointer": True,
        }
    ]
    for item in catalog_models(registry):
        if item.get("pool_id") not in (None, POOL_HS):
            continue
        run_id = str(item.get("run_id") or "")
        meta_path = registry / run_id / "metadata.json"
        model_path = registry / run_id / "model.pkl"
        rank_ic = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                rank_ic = (meta.get("walk_forward_oos") or {}).get("rank_ic_mean")
            except (json.JSONDecodeError, OSError):
                rank_ic = None
        choices.append(
            {
                "choice_id": run_id,
                "label": str(item.get("label") or run_id),
                "run_id": run_id,
                "rank_ic": rank_ic,
                "ready": model_path.is_file() and meta_path.is_file(),
                "is_champion_pointer": False,
            }
        )
    return choices


def get_active_hs_choice(registry_dir: str | Path = "mlops/registry") -> str:
    path = _registry(registry_dir) / _ACTIVE
    if not path.is_file():
        return CHOICE_CHAMPION
    try:
        choice = str(json.loads(path.read_text(encoding="utf-8")).get("choice_id") or CHOICE_CHAMPION)
    except (json.JSONDecodeError, OSError):
        return CHOICE_CHAMPION
    known = {item["choice_id"] for item in list_hs_choices(registry_dir)}
    if choice not in known:
        return CHOICE_CHAMPION
    return choice


def set_active_hs_choice(choice_id: str, registry_dir: str | Path = "mlops/registry") -> None:
    known = {item["choice_id"]: item for item in list_hs_choices(registry_dir)}
    if choice_id not in known:
        raise ValueError(f"不能切换到未登记的模型：{choice_id}")
    if not known[choice_id]["ready"]:
        raise ValueError(f"模型还没准备好：{choice_id}")
    path = _registry(registry_dir) / _ACTIVE
    path.write_text(
        json.dumps({"choice_id": choice_id, "pool_id": POOL_HS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_scoring_run_id(pool_id: str, registry_dir: str | Path = "mlops/registry") -> str | None:
    if pool_id == POOL_BJ:
        described = describe_champion(registry_dir, pool_id=POOL_BJ)
        return described["run_id"] if described.get("ready") else None
    if pool_id != POOL_HS:
        raise ValueError(f"未知 pool_id={pool_id}")
    choice = get_active_hs_choice(registry_dir)
    if choice == CHOICE_CHAMPION:
        return _champion_run_id(registry_dir)
    return choice


def load_scoring_model(registry_dir: str | Path = "mlops/registry", *, pool_id: str = POOL_HS):
    """扫描用。沪深看当前选择；北交所只读该池冠军。"""
    if pool_id == POOL_BJ or get_active_hs_choice(registry_dir) == CHOICE_CHAMPION:
        model, metadata = load_champion_model(registry_dir, pool_id=pool_id if pool_id == POOL_BJ else POOL_HS)
        metadata = dict(metadata)
        metadata["selection"] = "champion"
        return model, metadata
    if pool_id != POOL_HS:
        raise ValueError(f"未知 pool_id={pool_id}")
    run_id = get_active_hs_choice(registry_dir)
    registry = _registry(registry_dir)
    run_dir = registry / run_id
    meta_path = run_dir / "metadata.json"
    model_path = run_dir / "model.pkl"
    if not meta_path.is_file() or not model_path.is_file():
        raise FileNotFoundError(f"已选沪深模型 {run_id} 缺少 model.pkl 或 metadata.json")
    import pickle

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    with model_path.open("rb") as f:
        model = pickle.load(f)
    metadata = dict(metadata)
    metadata["pool_id"] = POOL_HS
    metadata["selection"] = "catalog"
    metadata["run_id"] = metadata.get("run_id") or run_id
    return model, metadata
