"""沪深打分模型切换的真实库探针。

对照：用户要求回归树按冠军规格接入，并可与现行冠军切换；北交所不串池；
冠军指针不被替换。验收：两次沪深扫描的 model_run_id 分别是冠军与回归树，
北交所扫描仍是北交所冠军，建议落库编号跟着切换走，探针结束恢复进入时的选择。

会写入 prediction_log / scan_run，并重写当日沪深 advice_log 快照（先回归树，最后写回冠军）。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from advice.model_selection import (
    CHOICE_CHAMPION,
    get_active_hs_choice,
    resolve_scoring_run_id,
    set_active_hs_choice,
)
from advice.reason_pack import build_reason_pack
import pandas as pd

REG = _ROOT / "mlops" / "registry"
CHAMPION_HS = "20260918_232953"
REGRESS = "20260922_150545_reg"
CHAMPION_BJ = "20260918_233000"
LOG_PATH = _ROOT / "docs" / "probe-hs-model-switch-20260922.md"


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.fail = 0
        self.ok = 0

    def emit(self, status: str, item: str, detail: str = "") -> None:
        line = f"[{dt.datetime.now():%H:%M:%S}] {status:6} {item}" + (f" — {detail}" if detail else "")
        print(line, flush=True)
        self.lines.append(line)
        if status == "OK":
            self.ok += 1
        elif status == "FAIL":
            self.fail += 1


def _top(df: pd.DataFrame, n: int = 10) -> list[str]:
    return df.head(n)["symbol"].astype(str).tolist()


def _latest_scan_model(conn, pool_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT model_run_id FROM scan_run
        WHERE pool_id = ?
        ORDER BY scanned_at DESC
        LIMIT 1
        """,
        [pool_id],
    ).fetchone()
    return row[0] if row else None


def _latest_advice_model(conn, pool_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT model_run_id FROM advice_log
        WHERE coalesce(pool_id, 'hs') = ?
        ORDER BY as_of DESC
        LIMIT 1
        """,
        [pool_id],
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    log = Log()
    champ_before = {
        name: (REG / name).read_text(encoding="utf-8")
        for name in ("champion.json", "champion_hs.json", "champion_bj.json")
        if (REG / name).is_file()
    }
    active_path = REG / "active_hs_model.json"
    active_before = active_path.read_text(encoding="utf-8") if active_path.is_file() else None

    from advice.scanner import run_scan
    from advice.advice_engine import generate_daily_advice
    from common.db import get_connection, init_schema

    try:
        set_active_hs_choice(CHOICE_CHAMPION, REG)
        if resolve_scoring_run_id("hs", REG) != CHAMPION_HS:
            log.emit("FAIL", "resolve champion", resolve_scoring_run_id("hs", REG) or "")
        else:
            log.emit("OK", "resolve champion", CHAMPION_HS)

        hs_champ = run_scan(top_n=30, registry_dir=str(REG), pool_id="hs", source="probe")
        full_c, _top_c, date_c = hs_champ
        log.emit("OK", "scan hs champion", f"asof={date_c} n={len(full_c)} top10={_top(full_c)}")

        set_active_hs_choice(REGRESS, REG)
        if resolve_scoring_run_id("hs", REG) != REGRESS:
            log.emit("FAIL", "resolve regress", str(resolve_scoring_run_id("hs", REG)))
        else:
            log.emit("OK", "resolve regress", REGRESS)
        if resolve_scoring_run_id("bj", REG) != CHAMPION_BJ:
            log.emit("FAIL", "bj ignores hs switch", str(resolve_scoring_run_id("bj", REG)))
        else:
            log.emit("OK", "bj stays champion", CHAMPION_BJ)

        hs_reg = run_scan(top_n=30, registry_dir=str(REG), pool_id="hs", source="probe")
        full_r, top_r, date_r = hs_reg
        overlap = len(set(_top(full_c)) & set(_top(full_r)))
        log.emit(
            "OK" if overlap < 10 else "FAIL",
            "hs top10 differ",
            f"overlap={overlap}/10 regress_top10={_top(full_r)}",
        )
        bad = top_r["reason_one_liner"].astype(str).str.contains("冠军模型").sum() if "reason_one_liner" in top_r.columns else -1
        if bad:
            log.emit("FAIL", "reason text", f"{int(bad)} 条仍写冠军模型")
        else:
            log.emit("OK", "reason text", "回归树扫描未把排序说成冠军模型")

        bj = run_scan(top_n=10, registry_dir=str(REG), pool_id="bj", source="probe")
        full_bj, _, date_bj = bj
        log.emit("OK", "scan bj", f"asof={date_bj} n={len(full_bj)} top5={_top(full_bj, 5)}")

        advice_r = generate_daily_advice(pool_id="hs")
        watch = advice_r.get("watchlist_cards") or []
        sized = [c for c in watch if c.get("size_shares") is not None]
        bad_lots = [
            c for c in sized
            if int(c.get("size_shares") or 0) % 100 != 0
        ]
        log.emit(
            "OK" if watch and not bad_lots else "FAIL",
            "advice regress",
            f"watch={len(watch)} sized={len(sized)} bad_lots={len(bad_lots)} as_of={advice_r.get('as_of')}",
        )
        conn = get_connection()
        init_schema(conn)
        try:
            mid = _latest_advice_model(conn, "hs")
        finally:
            conn.close()
        log.emit("OK" if mid == REGRESS else "FAIL", "advice_log while regress", str(mid))

        from advice.advice_engine import run_practice_cycle
        from advice.paper_broker import create_paper_account

        probe_account = create_paper_account(
            "live_prep_10k", note="probe-hs-model-switch-20260922", kind="human"
        )
        practice_r = run_practice_cycle(probe_account, pool_id="hs", simulate_scope="todos")
        sim_r = practice_r["simulation"]
        log.emit(
            "OK",
            "paper regress todos",
            f"account={probe_account} filled={sim_r['filled']} pending={sim_r['pending']} "
            f"rejected={sim_r['rejected']} reasons={sim_r['reason_counts']}",
        )

        set_active_hs_choice(CHOICE_CHAMPION, REG)
        practice_c = run_practice_cycle(probe_account, pool_id="hs", simulate_scope="todos")
        sim_c = practice_c["simulation"]
        log.emit(
            "OK",
            "paper champion todos",
            f"filled={sim_c['filled']} pending={sim_c['pending']} rejected={sim_c['rejected']} "
            f"reasons={sim_c['reason_counts']}",
        )
        advice_c = practice_c
        log.emit(
            "OK" if advice_c.get("watchlist_cards") else "FAIL",
            "advice champion restored",
            f"watch={len(advice_c.get('watchlist_cards') or [])} as_of={advice_c.get('as_of')}",
        )

        conn = get_connection()
        init_schema(conn)
        try:
            hs_model = _latest_scan_model(conn, "hs")
            bj_model = _latest_scan_model(conn, "bj")
            advice_model = _latest_advice_model(conn, "hs")
        finally:
            conn.close()
        log.emit("OK" if hs_model == CHAMPION_HS else "FAIL", "scan_run hs last", str(hs_model))
        log.emit("OK" if bj_model == CHAMPION_BJ else "FAIL", "scan_run bj last", str(bj_model))
        log.emit("OK" if advice_model == CHAMPION_HS else "FAIL", "advice_log hs", str(advice_model))
    finally:
        if active_before is None:
            if active_path.is_file() and get_active_hs_choice(REG) != CHOICE_CHAMPION:
                set_active_hs_choice(CHOICE_CHAMPION, REG)
        else:
            active_path.write_text(active_before, encoding="utf-8")

    champ_after = {
        name: (REG / name).read_text(encoding="utf-8")
        for name in champ_before
    }
    if champ_after == champ_before:
        log.emit("OK", "champion files untouched")
    else:
        log.emit("FAIL", "champion files changed")

    # 静态句子：无冲突时不得再写死「冠军模型」
    sample = build_reason_pack(pd.Series({"rank": 1, "pred_score": 0.1, "conflict_flag": "无冲突"}))
    if "冠军模型" in sample["one_liner"]:
        log.emit("FAIL", "reason pack static", sample["one_liner"])
    else:
        log.emit("OK", "reason pack static", sample["one_liner"])

    body = [
        "# 沪深打分模型切换探针 2026-09-22",
        "",
        f"失败 {log.fail}，通过 {log.ok}。",
        "",
        "```text",
        *log.lines,
        "```",
        "",
    ]
    LOG_PATH.write_text("\n".join(body), encoding="utf-8")
    print(f"wrote {LOG_PATH}", flush=True)
    return 1 if log.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
