"""实时探针：面板重建 / 双池重训 / 决策链功能（真实库）。

用法:
  python -m scripts.probe_panel_dual_retrain              # 全阶段
  python -m scripts.probe_panel_dual_retrain --stage preflight
  python -m scripts.probe_panel_dual_retrain --stage panel
  python -m scripts.probe_panel_dual_retrain --stage champions
  python -m scripts.probe_panel_dual_retrain --stage functions

对照: requirements-20260918-scan-pools-paper.md R2/R3；留痕追加到
docs/probe-panel-dual-retrain-live.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "docs" / "probe-panel-dual-retrain-live.md"
PANEL_PATH = ROOT / "data" / "feature_panel.parquet"
REG = ROOT / "mlops" / "registry"

# 重建后训练面板至少应覆盖到的完整月末（今日为 2026-09-18）
EXPECT_FULL_MONTH_END = dt.date(2026, 8, 31)


class Probe:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.ok = 0
        self.fail = 0
        self.warn = 0
        self.lines: list[str] = []

    def _emit(self, status: str, item: str, detail: str = "") -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] [{self.stage}] {status:7} {item}" + (f" — {detail}" if detail else "")
        print(msg, flush=True)
        self.lines.append(msg)
        if status == "OK":
            self.ok += 1
        elif status == "FAIL":
            self.fail += 1
        elif status == "WARN":
            self.warn += 1

    def ok_(self, item: str, detail: str = "") -> None:
        self._emit("OK", item, detail)

    def fail_(self, item: str, detail: str = "") -> None:
        self._emit("FAIL", item, detail)

    def warn_(self, item: str, detail: str = "") -> None:
        self._emit("WARN", item, detail)

    def append_log(self) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not LOG_PATH.exists()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            if is_new:
                f.write("# 面板重建 + 双池重训 实时探针\n\n")
            f.write(f"\n## {dt.datetime.now().isoformat(timespec='seconds')} · {self.stage}\n\n")
            for line in self.lines:
                f.write(f"- `{line}`\n")
            f.write(f"\n合计 ok={self.ok} fail={self.fail} warn={self.warn}\n")


def stage_preflight(p: Probe) -> None:
    from common.db import get_connection, init_schema
    from common.warehouse_readiness import assess_warehouse_readiness

    conn = get_connection()
    try:
        init_schema(conn)
        report = assess_warehouse_readiness(conn)
        if report.ready:
            p.ok_("warehouse_ready", str(report.metrics))
        else:
            p.fail_("warehouse_ready", f"blockers={report.blockers}")

        q_max = conn.execute(
            "SELECT max(trade_date) FROM daily_quotes WHERE adjust='qfq'"
        ).fetchone()[0]
        cal_max = conn.execute("SELECT max(trade_date) FROM trade_calendar").fetchone()[0]
        n_sym = conn.execute(
            "SELECT count(*) FROM universe WHERE is_delisted=FALSE"
        ).fetchone()[0]
        n_bj = conn.execute(
            "SELECT count(*) FROM universe WHERE exchange='bj' AND is_delisted=FALSE"
        ).fetchone()[0]
        # 8 月末是否在日历与行情覆盖内
        has_0831 = conn.execute(
            "SELECT count(*) FROM trade_calendar WHERE trade_date = DATE '2026-08-31'"
        ).fetchone()[0]
        q_0831 = conn.execute(
            """
            SELECT count(DISTINCT symbol) FROM daily_quotes
            WHERE adjust='qfq' AND trade_date = DATE '2026-08-31'
            """
        ).fetchone()[0]
        p.ok_("quotes_max", str(q_max))
        p.ok_("calendar_max", str(cal_max))
        p.ok_("universe", f"active={n_sym} bj={n_bj}")
        if has_0831:
            p.ok_("calendar_2026-08-31", "present")
        else:
            p.fail_("calendar_2026-08-31", "missing")
        if q_0831 >= int(n_sym * 0.7):
            p.ok_("quotes_2026-08-31_coverage", f"symbols={q_0831}")
        else:
            p.fail_("quotes_2026-08-31_coverage", f"symbols={q_0831} active={n_sym}")

        # 月末列表（区间内）
        months = conn.execute(
            """
            SELECT trade_date FROM (
              SELECT trade_date,
                     row_number() OVER (
                       PARTITION BY year(trade_date), month(trade_date)
                       ORDER BY trade_date DESC
                     ) AS rn
              FROM trade_calendar
              WHERE trade_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
            ) WHERE rn = 1
            ORDER BY trade_date
            """
        ).fetchall()
        p.ok_("rebalance_dates_2026YTD", ", ".join(str(r[0]) for r in months))
    finally:
        conn.close()

    if PANEL_PATH.is_file():
        import pandas as pd

        df = pd.read_parquet(PANEL_PATH, columns=["trade_date"])
        tmax = pd.to_datetime(df["trade_date"]).max().date()
        p.ok_("panel_on_disk_before", f"max={tmax} rows={len(df)}")
    else:
        p.warn_("panel_on_disk_before", "missing")


def stage_panel(p: Probe) -> None:
    import pandas as pd

    if not PANEL_PATH.is_file():
        p.fail_("panel_file", "missing")
        return
    df = pd.read_parquet(PANEL_PATH, columns=["trade_date", "exchange", "symbol"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    tmax = df["trade_date"].max().date()
    tmin = df["trade_date"].min().date()
    n_dates = df["trade_date"].nunique()
    last5 = sorted(df["trade_date"].dt.date.unique())[-5:]
    p.ok_("panel_range", f"{tmin} ~ {tmax} dates={n_dates} rows={len(df)}")
    p.ok_("panel_last5", str(last5))
    if tmax < EXPECT_FULL_MONTH_END:
        p.fail_("panel_covers_aug_month_end", f"max={tmax} need>={EXPECT_FULL_MONTH_END}")
    else:
        p.ok_("panel_covers_aug_month_end", f"max={tmax}")
    for ex in ("sh", "sz", "bj"):
        sub = df[df["exchange"] == ex]
        if sub.empty:
            p.fail_(f"panel_exchange_{ex}", "empty")
        else:
            p.ok_(
                f"panel_exchange_{ex}",
                f"rows={len(sub)} max={sub['trade_date'].max().date()}",
            )


def stage_champions(p: Probe) -> None:
    for pool, fname in (("hs", "champion_hs.json"), ("bj", "champion_bj.json")):
        path = REG / fname
        if not path.is_file():
            p.fail_(f"champion_{pool}", "file missing")
            continue
        ptr = json.loads(path.read_text(encoding="utf-8"))
        rid = ptr.get("run_id")
        meta_path = REG / str(rid) / "metadata.json"
        if not meta_path.is_file():
            p.fail_(f"champion_{pool}_meta", f"run={rid} missing metadata")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dr = meta.get("date_range")
        p.ok_(
            f"champion_{pool}",
            f"run={rid} date_range={dr} rows={meta.get('n_rows_total')} "
            f"wf={meta.get('walk_forward_oos', {}).get('rank_ic_mean')} "
            f"promoted={ptr.get('reason', '')[:40]}",
        )
        if dr and len(dr) == 2:
            end = dt.date.fromisoformat(str(dr[1])[:10])
            if end < EXPECT_FULL_MONTH_END:
                p.warn_(f"champion_{pool}_stale_vs_aug", f"end={end}")
            else:
                p.ok_(f"champion_{pool}_fresh_vs_aug", f"end={end}")


def stage_functions(p: Probe) -> None:
    from advice.champion_registry import load_champion_model
    from advice.scan_archive import persistence_hit_counts, symbol_rank_history
    from advice.scanner import run_scan
    from common.db import get_connection, init_schema
    import subprocess

    try:
        m_hs, meta_hs = load_champion_model(str(REG), pool_id="hs")
        m_bj, meta_bj = load_champion_model(str(REG), pool_id="bj")
        p.ok_("load_champions", f"hs={meta_hs['run_id']} bj={meta_bj['run_id']}")
    except Exception as e:
        p.fail_("load_champions", str(e))
        return

    try:
        full_hs, top_hs, td_hs = run_scan(top_n=20, pool_id="hs", source="probe")
        n_bj_in_hs = int((full_hs["exchange"] == "bj").sum()) if "exchange" in full_hs.columns else -1
        if n_bj_in_hs == 0 and len(full_hs) > 100:
            p.ok_("scan_hs", f"n={len(full_hs)} asof={td_hs} top={len(top_hs)}")
        else:
            p.fail_("scan_hs", f"n={len(full_hs)} bj_leak={n_bj_in_hs} asof={td_hs}")
    except Exception as e:
        p.fail_("scan_hs", str(e))

    try:
        full_bj, top_bj, td_bj = run_scan(top_n=20, pool_id="bj", source="probe")
        n_non = int((full_bj["exchange"] != "bj").sum()) if "exchange" in full_bj.columns else -1
        if n_non == 0 and len(full_bj) > 50:
            p.ok_("scan_bj", f"n={len(full_bj)} asof={td_bj} top={len(top_bj)}")
        else:
            p.fail_("scan_bj", f"n={len(full_bj)} non_bj={n_non} asof={td_bj}")
    except Exception as e:
        p.fail_("scan_bj", str(e))

    conn = get_connection()
    try:
        init_schema(conn)
        hits = persistence_hit_counts(conn, pool_id="hs", lookback_runs=10, top_k=50)
        if hits is not None and not hits.empty:
            sym = str(hits.iloc[0]["symbol"])
            hist = symbol_rank_history(conn, sym, limit=5)
            p.ok_("persistence_rank", f"hits={len(hits)} sample={sym} hist={len(hist)}")
        else:
            p.warn_("persistence_rank", "hs hits empty (may need more scans)")
    except Exception as e:
        p.fail_("persistence_rank", str(e))
    finally:
        conn.close()

    py = ROOT / ".venv" / "Scripts" / "python.exe"
    exe = str(py) if py.is_file() else sys.executable
    proc = subprocess.run(
        [exe, "-m", "scripts.test_paper_broker"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        p.ok_("paper_broker_tests", "exit 0")
    else:
        p.fail_("paper_broker_tests", (proc.stdout + proc.stderr)[-500:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=["preflight", "panel", "champions", "functions", "all"],
        default="all",
    )
    args = parser.parse_args()
    stages = (
        ["preflight", "panel", "champions", "functions"]
        if args.stage == "all"
        else [args.stage]
    )
    total_fail = 0
    for s in stages:
        p = Probe(s)
        if s == "preflight":
            stage_preflight(p)
        elif s == "panel":
            stage_panel(p)
        elif s == "champions":
            stage_champions(p)
        elif s == "functions":
            stage_functions(p)
        p.append_log()
        print(
            f"==> stage {s}: ok={p.ok} fail={p.fail} warn={p.warn}",
            flush=True,
        )
        total_fail += p.fail
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
