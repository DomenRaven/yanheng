"""规格 M1 + R2：scanner 只读分池冠军，禁止回退最新文件夹；bj 不得用 hs 代打。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from advice.champion_registry import filter_snapshot_by_pool, load_champion_model
from advice.scanner import _load_champion_model
import pandas as pd


def test_missing_champion_fails() -> None:
    d = Path(tempfile.mkdtemp()) / "registry"
    d.mkdir()
    (d / "20990101_000000").mkdir()
    (d / "20990101_000000" / "metadata.json").write_text(
        json.dumps({"run_id": "20990101_000000", "trained_at": "x"}),
        encoding="utf-8",
    )
    try:
        _load_champion_model(str(d), pool_id="hs")
        raise AssertionError("缺少冠军指针时不应静默加载最新 run")
    except FileNotFoundError as exc:
        msg = str(exc)
        assert "champion" in msg.lower()
        assert "禁止" in msg or "期望" in msg
    print("OK missing champion raises")


def test_bj_does_not_fallback_to_hs() -> None:
    d = Path(tempfile.mkdtemp()) / "registry"
    d.mkdir()
    # 仅有 hs 指针
    (d / "champion_hs.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    (d / "champion.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    try:
        load_champion_model(str(d), pool_id="bj")
        raise AssertionError("bj 不应回退到 hs/legacy champion")
    except FileNotFoundError as exc:
        assert "bj" in str(exc)
    print("OK bj no hs fallback")


def test_filter_pool() -> None:
    snap = pd.DataFrame(
        {
            "symbol": ["000001", "600000", "920165", "689009"],
            "exchange": ["sz", "sh", "bj", "sh"],
        }
    )
    hs = filter_snapshot_by_pool(snap, "hs")
    bj = filter_snapshot_by_pool(snap, "bj")
    assert set(hs["symbol"]) == {"000001", "600000", "689009"}
    assert set(bj["symbol"]) == {"920165"}
    print("OK filter pool")


def test_production_hs_champion_exists() -> None:
    champ = _ROOT / "mlops" / "registry" / "champion_hs.json"
    legacy = _ROOT / "mlops" / "registry" / "champion.json"
    assert champ.is_file() or legacy.is_file()
    path = champ if champ.is_file() else legacy
    data = json.loads(path.read_text(encoding="utf-8"))
    run_id = data["run_id"]
    run_dir = _ROOT / "mlops" / "registry" / run_id
    assert (run_dir / "model.pkl").is_file(), run_dir
    model, meta = _load_champion_model(str(_ROOT / "mlops" / "registry"), pool_id="hs")
    assert meta["run_id"] == run_id
    print("OK production hs champion", run_id)


if __name__ == "__main__":
    test_missing_champion_fails()
    test_bj_does_not_fallback_to_hs()
    test_filter_pool()
    test_production_hs_champion_exists()
    print("test_champion_only: passed")
