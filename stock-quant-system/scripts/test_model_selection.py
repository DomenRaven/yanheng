"""沪深打分切换：默认冠军；只能切到目录里的模型；不改冠军指针；北交所不切换。"""
from __future__ import annotations

import json
import pickle
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from advice.model_selection import (
    CHOICE_CHAMPION,
    get_active_hs_choice,
    list_hs_choices,
    load_scoring_model,
    register_selectable,
    resolve_scoring_run_id,
    set_active_hs_choice,
)


class _FakeModel:
    def predict(self, frame):
        return [0.1] * len(frame)


def _write_run(registry: Path, run_id: str) -> None:
    run = registry / run_id
    run.mkdir()
    (run / "metadata.json").write_text(
        json.dumps({
            "run_id": run_id,
            "feature_cols": ["factor_bp"],
            "walk_forward_oos": {"rank_ic_mean": 0.07},
        }),
        encoding="utf-8",
    )
    with open(run / "model.pkl", "wb") as f:
        pickle.dump(_FakeModel(), f)


def test_default_is_champion_and_switch_does_not_touch_pointer() -> None:
    registry = Path(tempfile.mkdtemp()) / "registry"
    registry.mkdir()
    _write_run(registry, "champ")
    _write_run(registry, "reg")
    _write_run(registry, "bjchamp")
    (registry / "champion_hs.json").write_text(json.dumps({"run_id": "champ"}), encoding="utf-8")
    (registry / "champion.json").write_text(json.dumps({"run_id": "champ"}), encoding="utf-8")
    (registry / "champion_bj.json").write_text(json.dumps({"run_id": "bjchamp"}), encoding="utf-8")
    before = (registry / "champion_hs.json").read_text(encoding="utf-8")

    assert get_active_hs_choice(registry) == CHOICE_CHAMPION
    assert resolve_scoring_run_id("hs", registry) == "champ"
    assert resolve_scoring_run_id("bj", registry) == "bjchamp"

    register_selectable(registry, run_id="reg", label="回归树", objective="regression")
    ids = [c["choice_id"] for c in list_hs_choices(registry)]
    assert ids == [CHOICE_CHAMPION, "reg"]

    set_active_hs_choice("reg", registry)
    assert get_active_hs_choice(registry) == "reg"
    _model, meta = load_scoring_model(registry, pool_id="hs")
    assert meta["run_id"] == "reg"
    assert meta["selection"] == "catalog"
    _bj, bj_meta = load_scoring_model(registry, pool_id="bj")
    assert bj_meta["run_id"] == "bjchamp"
    assert bj_meta["selection"] == "champion"
    assert (registry / "champion_hs.json").read_text(encoding="utf-8") == before

    try:
        set_active_hs_choice("not-registered", registry)
        raise AssertionError("未登记模型不应能切换")
    except ValueError:
        pass
    print("OK model selection")


if __name__ == "__main__":
    test_default_is_champion_and_switch_does_not_touch_pointer()
    print("test_model_selection: passed")
