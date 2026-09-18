"""
定期重训练脚本 + 冠军-挑战者晋升门禁（对齐 `.cursor/rules/quant-dev-loop.mdc` 的CD4ML门禁，
呼应计划文档 `个人炒股辅助本地应用_76077b1a.plan.md` 第7节已确认的"每周末重训一次"节奏，
见 `config.yaml` 的 `model.retrain_cadence: weekly`）。

本脚本不内置定时器——Windows没有内建cron，定时触发交给系统任务计划程序调用本脚本；
脚本本身只负责"重训一次 + 做冠军替换判断"这一个原子操作，可以手动跑也可以被调度器跑。

流程：
1. 用最新数据重建特征面板(`research.panel.build_feature_panel`)覆盖 `data/feature_panel.parquet`
2. 训练一个新模型（挑战者），写入 `mlops/registry/<run_id>/`（复用 `research.train_lightgbm.
   run_training`，与Phase1训练走同一套代码路径，不重复实现）
3. 读当前冠军（`mlops/registry/champion.json`）的 walk-forward 样本外 RankIC，与挑战者
   用同一套验证协议算出的指标比较
4. 冠军替换规则：
   - 没有冠军（首次）-> 挑战者直接晋升
   - 挑战者 walk_forward.rank_ic_mean >= 冠军 - tolerance（默认0.01）-> 晋升
     （不要求"严格更优"，因为每周用更多新数据重训本身有新鲜度价值，只要求"没有明显
     变差"；tolerance写在metadata里公开，不是暗中放水）
   - 否则维持冠军，挑战者模型文件仍保留在registry里（不删除，留痕，可以人工复核）
5. 每次决策追加写入 `mlops/registry/promotion_log.jsonl`，形成可追溯的模型版本演进历史
   （对齐计划文档Phase2验收标准"能看到模型版本演进历史"）
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

logger = logging.getLogger("mlops.retrain_schedule")

_DEFAULT_TOLERANCE = 0.01


def _read_champion(registry_dir: Path, pool_id: str | None = None) -> dict | None:
    """读分池冠军；pool_id=None 时读 legacy champion.json（兼容旧调用）。"""
    if pool_id:
        path = registry_dir / f"champion_{pool_id}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        if pool_id == "hs":
            legacy = registry_dir / "champion.json"
            if legacy.exists():
                with open(legacy, encoding="utf-8") as f:
                    return json.load(f)
        return None
    champion_file = registry_dir / "champion.json"
    if not champion_file.exists():
        return None
    with open(champion_file, encoding="utf-8") as f:
        return json.load(f)


def _write_champion(registry_dir: Path, payload: dict, pool_id: str | None = None) -> None:
    if pool_id:
        path = registry_dir / f"champion_{pool_id}.json"
        payload = {**payload, "pool_id": pool_id}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if pool_id == "hs":
            # 同步 legacy 指针，兼容旧 UI/审计
            with open(registry_dir / "champion.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        return
    with open(registry_dir / "champion.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_metadata(registry_dir: Path, run_id: str) -> dict:
    with open(registry_dir / run_id / "metadata.json", encoding="utf-8") as f:
        return json.load(f)


def _append_promotion_log(registry_dir: Path, entry: dict) -> None:
    log_file = registry_dir / "promotion_log.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def decide_promotion(
    challenger_metadata: dict,
    champion_metadata: dict | None,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> tuple[bool, str]:
    """返回 (是否晋升, 理由文本)。只用 walk_forward 样本外指标做判断——
    purged_kfold会系统性偏高（见 research/train_lightgbm.py 模块docstring），
    不能作为晋升依据。"""
    challenger_ic = challenger_metadata.get("walk_forward_oos", {}).get("rank_ic_mean")
    if challenger_ic is None:
        return False, "挑战者walk_forward样本外指标缺失，视为训练失败，不晋升"
    if champion_metadata is None:
        return True, f"当前无冠军模型，挑战者(RankIC={challenger_ic})直接晋升为首个冠军"
    champion_ic = champion_metadata.get("walk_forward_oos", {}).get("rank_ic_mean")
    if champion_ic is None:
        return True, "冠军模型指标缺失(异常情况)，挑战者晋升"
    if challenger_ic >= champion_ic - tolerance:
        return True, (
            f"挑战者RankIC={challenger_ic} >= 冠军RankIC={champion_ic} - 容差{tolerance}，"
            f"晋升（用更新数据训练的模型优先，只要没有明显劣化）"
        )
    return False, (
        f"挑战者RankIC={challenger_ic} 明显低于冠军RankIC={champion_ic}(容差{tolerance})，"
        f"维持冠军，挑战者模型留痕但不启用"
    )


def bootstrap_champion(registry_dir: str = "mlops/registry") -> dict:
    """把已有（Phase1训练产出、还没有champion.json机制时留下）的最新一次训练run
    直接设为初始冠军，不重新训练——复用存量，避免"为了建立机制而做一次无意义的重复训练"。
    之后的 run_weekly_retrain 才会走真正的挑战者对比流程。"""
    registry_path = Path(registry_dir)
    if (registry_path / "champion.json").exists():
        raise RuntimeError("champion.json 已存在，不要重复bootstrap；如需重置请手动删除该文件")
    runs = sorted(registry_path.glob("*/metadata.json"))
    if not runs:
        raise FileNotFoundError(f"{registry_dir} 下没有任何已训练模型，无法bootstrap")
    run_id = runs[-1].parent.name
    metadata = _read_metadata(registry_path, run_id)
    payload = {
        "run_id": run_id,
        "promoted_at": dt.datetime.now().isoformat(),
        "reason": "Phase2引入冠军-挑战者机制时的初始冠军：复用Phase1已训练产出，未重新训练",
        "previous_champion": None,
    }
    with open(registry_path / "champion.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _append_promotion_log(
        registry_path,
        {
            "decision_time": payload["promoted_at"],
            "challenger_run_id": run_id,
            "challenger_walk_forward_rank_ic": metadata.get("walk_forward_oos", {}).get("rank_ic_mean"),
            "champion_run_id_before": None,
            "champion_walk_forward_rank_ic_before": None,
            "promoted": True,
            "reason": payload["reason"],
            "champion_run_id_after": run_id,
        },
    )
    logger.info("已bootstrap初始冠军 run_id=%s", run_id)
    return payload


def run_weekly_retrain(
    registry_dir: str = "mlops/registry",
    panel_start_date: str = "20160101",
    tolerance: float = _DEFAULT_TOLERANCE,
    rebuild_panel: bool = True,
    *,
    pool_id: str | None = None,
) -> dict:
    from research.panel import build_feature_panel
    from research.train_lightgbm import run_training

    registry_path = Path(registry_dir)
    registry_path.mkdir(parents=True, exist_ok=True)
    panel_path = "data/feature_panel.parquet"

    if rebuild_panel:
        logger.info("重建特征面板（覆盖 %s）...", panel_path)
        panel = build_feature_panel(start_date=panel_start_date)
        panel.to_parquet(panel_path)
        logger.info("面板重建完成: %d 行, %d 截面日", len(panel), panel["trade_date"].nunique())
    else:
        logger.info("跳过面板重建，直接用现有 %s 训练挑战者", panel_path)

    logger.info("训练挑战者模型 pool=%s ...", pool_id or "all")
    challenger_metadata = run_training(panel_path=panel_path, registry_dir=registry_dir, pool_id=pool_id)
    challenger_run_id = challenger_metadata["run_id"]
    logger.info("挑战者训练完成 run_id=%s，walk_forward=%s", challenger_run_id, challenger_metadata["walk_forward_oos"])

    champion = _read_champion(registry_path, pool_id=pool_id)
    champion_run_id = champion["run_id"] if champion else None
    champion_metadata = _read_metadata(registry_path, champion_run_id) if champion_run_id else None

    promote, reason = decide_promotion(challenger_metadata, champion_metadata, tolerance)
    decision_time = dt.datetime.now().isoformat()

    if promote:
        _write_champion(
            registry_path,
            {
                "run_id": challenger_run_id,
                "promoted_at": decision_time,
                "reason": reason,
                "previous_champion": champion_run_id,
            },
            pool_id=pool_id,
        )
        logger.info("挑战者 %s 已晋升为冠军(pool=%s): %s", challenger_run_id, pool_id, reason)
    else:
        logger.info("挑战者 %s 未晋升，维持冠军 %s: %s", challenger_run_id, champion_run_id, reason)

    _append_promotion_log(
        registry_path,
        {
            "decision_time": decision_time,
            "pool_id": pool_id,
            "challenger_run_id": challenger_run_id,
            "challenger_walk_forward_rank_ic": challenger_metadata.get("walk_forward_oos", {}).get("rank_ic_mean"),
            "champion_run_id_before": champion_run_id,
            "champion_walk_forward_rank_ic_before": (champion_metadata or {}).get("walk_forward_oos", {}).get("rank_ic_mean"),
            "promoted": promote,
            "reason": reason,
            "champion_run_id_after": challenger_run_id if promote else champion_run_id,
        },
    )

    return {
        "pool_id": pool_id,
        "challenger_run_id": challenger_run_id,
        "promoted": promote,
        "reason": reason,
        "champion_run_id": challenger_run_id if promote else champion_run_id,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="定期重训练 + 冠军-挑战者晋升门禁")
    parser.add_argument("--registry-dir", type=str, default="mlops/registry")
    parser.add_argument("--panel-start", type=str, default="20160101")
    parser.add_argument("--tolerance", type=float, default=_DEFAULT_TOLERANCE)
    parser.add_argument("--no-rebuild-panel", action="store_true", help="跳过面板重建，直接用现有parquet训练(调试用)")
    parser.add_argument("--bootstrap", action="store_true", help="把已有最新run设为初始冠军，不重新训练")
    parser.add_argument(
        "--pool",
        choices=["hs", "bj"],
        default=None,
        help="分池训练并写入 champion_{pool}.json；省略则写 legacy champion.json（全市场面板）",
    )
    parser.add_argument(
        "--pools",
        action="store_true",
        help="依次训练 hs 与 bj（bj 样本短时可能 Walk-Forward 折数不足，见报告局限）",
    )
    args = parser.parse_args()

    if args.bootstrap:
        print(bootstrap_champion(args.registry_dir))
        raise SystemExit(0)

    if args.pools:
        out = {}
        for pid in ("hs", "bj"):
            out[pid] = run_weekly_retrain(
                registry_dir=args.registry_dir,
                panel_start_date=args.panel_start,
                tolerance=args.tolerance,
                rebuild_panel=(pid == "hs") and (not args.no_rebuild_panel),
                pool_id=pid,
            )
        print(out)
        raise SystemExit(0)

    result = run_weekly_retrain(
        registry_dir=args.registry_dir,
        panel_start_date=args.panel_start,
        tolerance=args.tolerance,
        rebuild_panel=not args.no_rebuild_panel,
        pool_id=args.pool,
    )
    print(result)
