"""流水线级断点：记录已完成步骤，配合各 ingestion 模块行级增量 upsert。

断点语义：
  - 已完成且 status=ok 的步骤在 --resume 时整步跳过（不重复跑 HTTP）。
  - 未完成或中断的步骤重跑时，由 quotes/fundamentals 等模块按库内最大日期/报告期增量，
    upsert 主键去重，不重复、不混淆。
  - 状态写在仓库旁的 JSON，避免长任务占 DuckDB 时无法读写状态表。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.db import get_db_path

logger = logging.getLogger("ingestion_engine.pipeline_state")

_STATE_BASENAME = "ingestion_pipeline_state.json"


def state_file_path() -> Path:
    return get_db_path().parent / _STATE_BASENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_state() -> dict[str, Any] | None:
    path = state_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("无法读取流水线状态 %s: %s", path, exc)
        return None


def save_state(state: dict[str, Any]) -> None:
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def new_run(args_snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8],
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "running",
        "args": args_snapshot,
        "steps": {},
        "substeps": {},
    }


def mark_substep(state: dict[str, Any], step: str, substep: str, record: dict[str, Any]) -> None:
    """步内子任务断点（如 tushare_market_data.daily_basic），配合 --resume 避免整步重跑。"""
    substeps = state.setdefault("substeps", {})
    substeps.setdefault(step, {})[substep] = {**record, "finished_at": _now_iso()}
    save_state(state)


def substep_is_ok(state: dict[str, Any] | None, step: str, substep: str) -> bool:
    if not state:
        return False
    rec = state.get("substeps", {}).get(step, {}).get(substep)
    return bool(rec and rec.get("status") == "ok")


def clear_substeps(state: dict[str, Any], step: str | None = None) -> None:
    if step is None:
        state["substeps"] = {}
    else:
        state.get("substeps", {}).pop(step, None)
    save_state(state)


def mark_step(state: dict[str, Any], step: str, record: dict[str, Any]) -> None:
    state["steps"][step] = {**record, "finished_at": _now_iso()}
    save_state(state)


def mark_run_finished(state: dict[str, Any], status: str = "completed") -> None:
    state["status"] = status
    save_state(state)


def steps_to_skip_on_resume(state: dict[str, Any], step_order: list[str]) -> set[str]:
    """本 run_id 下已成功的步骤；resume 时跳过。"""
    done: set[str] = set()
    for step in step_order:
        rec = state.get("steps", {}).get(step)
        if rec and rec.get("status") == "ok":
            done.add(step)
    return done


def args_compatible(state: dict[str, Any], args_snapshot: dict[str, Any]) -> bool:
    """resume 时 only/skip 须与上次一致，避免混跑不同编排。"""
    prev = state.get("args") or {}
    keys = ("only", "skip", "quotes_limit", "fundamentals_limit", "corporate_actions_limit", "tushare_limit")
    return all(prev.get(k) == args_snapshot.get(k) for k in keys)


def _parse_state_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
    return ts


def clear_stale_pipeline_lock(
    *,
    empty_max_age_minutes: float = 5.0,
    max_age_hours: float | None = None,
) -> dict[str, Any] | None:
    """清掉「进程已死、断点仍 running」的假锁，避免启动自检永久 BUSY。

    只动两类僵尸：
    - ``steps`` 为空且超过 ``empty_max_age_minutes``（计划任务被 stderr 杀掉时常这样）
    - ``interrupted`` 且 ``steps`` 为空

    有真实步骤进度的 ``running`` **不**按墙钟超时清除——全市场 quotes 单步可达数小时，
    应人工 ``--resume`` 或等进程自己写完。``max_age_hours`` 仅保留调用兼容，不再用于
    有进度的 running 断点。
    """
    _ = max_age_hours
    state = load_state()
    if not state or state.get("status") not in ("running", "interrupted"):
        return None

    steps = state.get("steps") or {}
    updated = _parse_state_ts(state.get("updated_at") or state.get("started_at"))
    age_h: float | None = None
    if updated is not None:
        age_h = (datetime.now(updated.tzinfo) - updated).total_seconds() / 3600.0

    empty = len(steps) == 0
    empty_stale = (
        empty
        and age_h is not None
        and age_h >= (empty_max_age_minutes / 60.0)
    )
    interrupted_empty = empty and state.get("status") == "interrupted"
    if not (empty_stale or interrupted_empty):
        logger.info(
            "灌库断点仍视为有效 run_id=%s status=%s steps=%d age_h=%s",
            state.get("run_id"),
            state.get("status"),
            len(steps),
            None if age_h is None else round(age_h, 2),
        )
        return None

    mark_run_finished(state, status="aborted_stale")
    logger.warning(
        "已清除陈旧灌库断点 run_id=%s empty=%s age_h=%s → aborted_stale",
        state.get("run_id"),
        empty,
        None if age_h is None else round(age_h, 2),
    )
    return {
        "action": "aborted_stale",
        "run_id": state.get("run_id"),
        "empty": empty,
        "age_h": None if age_h is None else round(age_h, 2),
    }
