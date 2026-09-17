"""
实时监测 run_data_update 进度与整体 ETA（只读读日志与断点文件，不写 DuckDB）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "ingestion_pipeline_state.json"
LOG_DIR = ROOT / "data" / "logs"

STEP_ORDER = [
    "universe",
    "quotes",
    "fundamentals",
    "income_statement",
    "corporate_actions",
    "reference_data",
    "market_data",
    "tushare_prices",
    "tushare_market_data",
    "tushare_behavior",
]
PARALLEL = frozenset({"fundamentals", "income_statement", "corporate_actions"})

# 全量无 limit 时的保守缺省（秒）；有已完成步则用实测替换同名单步预估
# 2026-09-16/17 全量实测（run_id 20260916-150917-c62d0993）校准
STEP_PRIOR_S: dict[str, float] = {
    "universe": 180.0,
    "quotes": 5200.0,
    "fundamentals": 6200.0,
    "income_statement": 4000.0,
    "corporate_actions": 9100.0,
    "reference_data": 160.0,
    "market_data": 45.0,
    "tushare_prices": 4100.0,
    "tushare_market_data": 5200.0,
    "tushare_behavior": 11200.0,
}

STEP_LABEL = {
    "universe": "股票池",
    "quotes": "日 K 行情",
    "fundamentals": "财务比率",
    "income_statement": "利润表",
    "corporate_actions": "股本/分红",
    "reference_data": "披露日历/行业",
    "market_data": "交易日历/指数",
    "tushare_prices": "复权因子等",
    "tushare_market_data": "日频基本面/涨跌停",
    "tushare_behavior": "资金流/龙虎榜等",
}

TQDM_RE = re.compile(
    r"(?P<desc>[\w\[\]=_-]+):\s*\d+%\|[^|]*\|\s*(?P<cur>\d+)/(?P<tot>\d+)\s*\[(?P<bar>[^\]]+)\]"
)
STEP_RE = re.compile(r"=== Step (\w+) ===")
PARALLEL_RE = re.compile(r"=== 并行 HTTP: (.+) ===")
PROGRESS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] 进度 (\d+)/(\d+)"
)
DONE_RE = re.compile(r"(\w+) 完成，耗时 ([\d.]+): (ok|failed)")
RUN_DONE_RE = re.compile(r"run_data_update 结束")


def _parse_tqdm_remain(bar: str) -> float | None:
    # 00:30<58:05,  1.58it/s 或 01:26<57:44,  1.56it/s
    m = re.search(r"<(\d+):(\d{2})", bar)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _parse_rate(bar: str) -> float | None:
    m = re.search(r"([\d.]+)it/s", bar)
    return float(m.group(1)) if m else None


def _latest_log(explicit: Path | None) -> Path | None:
    if explicit and explicit.is_file():
        return explicit
    candidates = sorted(LOG_DIR.glob("run_data_update*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _read_log_bytes(path: Path, max_bytes: int = 400_000) -> bytes:
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= max_bytes:
            return f.read()
        start = size - max_bytes
        if start % 2:
            start += 1
        f.seek(start)
        return f.read()


def _decode_log(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[: min(400, len(raw))]:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _tail_text(path: Path, max_bytes: int = 400_000) -> str:
    return _decode_log(_read_log_bytes(path, max_bytes))


def parse_log(text: str) -> dict:
    current_step: str | None = None
    parallel_steps: list[str] = []
    tqdm_by_desc: dict[str, dict] = {}
    last_progress: tuple[datetime, int, int] | None = None

    for line in text.splitlines():
        sm = STEP_RE.search(line)
        if sm:
            current_step = sm.group(1)
            parallel_steps = []
        pm = PARALLEL_RE.search(line)
        if pm:
            parallel_steps = [s.strip() for s in pm.group(1).split("||")]
            current_step = parallel_steps[0] if parallel_steps else current_step
        tm = TQDM_RE.search(line)
        if tm:
            desc = tm.group("desc")
            cur, tot = int(tm.group("cur")), int(tm.group("tot"))
            bar = tm.group("bar")
            remain = _parse_tqdm_remain(bar)
            rate = _parse_rate(bar)
            tqdm_by_desc[desc] = {
                "cur": cur,
                "tot": tot,
                "remain_s": remain,
                "rate": rate,
            }
        pr = PROGRESS_RE.search(line)
        if pr:
            ts = datetime.strptime(pr.group(1), "%Y-%m-%d %H:%M:%S")
            last_progress = (ts, int(pr.group(2)), int(pr.group(3)))
        if RUN_DONE_RE.search(line):
            current_step = None

    desc_to_step = {
        "quotes_batch": "quotes",
        "fundamentals_batch": "fundamentals",
        "income_statement_batch[tushare]": "income_statement",
        "corporate_actions": "corporate_actions",
        "sync_adj_factor": "tushare_prices",
        "sync_fundamentals_bse_tushare": "fundamentals",
        "sync_bse_cdr_qfq": "tushare_prices",
    }
    if not current_step and tqdm_by_desc:
        for desc in tqdm_by_desc:
            if desc in desc_to_step:
                current_step = desc_to_step[desc]
                break

    return {
        "current_step": current_step,
        "parallel_steps": parallel_steps,
        "tqdm_by_desc": tqdm_by_desc,
        "last_progress": last_progress,
        "finished": RUN_DONE_RE.search(text) is not None,
    }


def _step_eta_from_tqdm(step: str, parsed: dict) -> float | None:
    desc_map = {
        "quotes": "quotes_batch",
        "fundamentals": "fundamentals_batch",
        "income_statement": "income_statement_batch[tushare]",
        "corporate_actions": "corporate_actions",
        "tushare_prices": "sync_adj_factor",
    }
    desc = desc_map.get(step)
    if desc and desc in parsed["tqdm_by_desc"]:
        t = parsed["tqdm_by_desc"][desc]
        if t.get("remain_s") is not None:
            return float(t["remain_s"])
        if t.get("rate") and t["tot"] > t["cur"]:
            return (t["tot"] - t["cur"]) / t["rate"]
    # 任意 tqdm 行匹配 step 名前缀
    for d, t in parsed["tqdm_by_desc"].items():
        if step in d or d.startswith("sync_") and step == "tushare_prices" and "adj" in d:
            if t.get("remain_s") is not None:
                return float(t["remain_s"])
    return None


def estimate_remaining_seconds(state: dict, parsed: dict) -> tuple[float, str]:
    steps_done = {
        k: v for k, v in state.get("steps", {}).items() if v.get("status") == "ok"
    }
    priors = dict(STEP_PRIOR_S)
    for k, v in steps_done.items():
        if v.get("elapsed_s"):
            priors[k] = float(v["elapsed_s"])

    remaining_steps: list[str] = []
    for s in STEP_ORDER:
        if s in steps_done:
            continue
        remaining_steps.append(s)

    if parsed.get("finished") or state.get("status") == "completed":
        return 0.0, "流水线已结束"

    if not remaining_steps and state.get("status") != "running":
        return 0.0, "无待执行步骤"

    total = 0.0
    notes: list[str] = []
    i = 0
    while i < len(remaining_steps):
        s = remaining_steps[i]
        if s in PARALLEL:
            bundle = []
            while i < len(remaining_steps) and remaining_steps[i] in PARALLEL:
                bundle.append(remaining_steps[i])
                i += 1
            etas = []
            for b in bundle:
                live = _step_eta_from_tqdm(b, parsed)
                etas.append(live if live is not None else priors[b])
            total += max(etas)
            notes.append(f"并行({','.join(bundle)})≈{max(etas)/60:.0f}分")
            continue

        live = _step_eta_from_tqdm(s, parsed)
        is_current = s == parsed.get("current_step") or (
            s == remaining_steps[0] and parsed.get("tqdm_by_desc")
        )
        if is_current:
            add = live if live is not None else priors[s]
            if live is not None:
                notes.append(f"当前步 {s} tqdm≈{live/60:.0f}分")
            else:
                notes.append(f"当前步 {s} 预估≈{add/60:.0f}分")
        else:
            add = priors[s]
            notes.append(f"{s} 预估≈{add/60:.0f}分")
        total += add
        i += 1

    return total, "; ".join(notes[:4]) + ("…" if len(notes) > 4 else "")


def _fmt_td(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    return str(timedelta(seconds=int(seconds)))


def render(state: dict, log_path: Path, parsed: dict, remain_s: float, note: str) -> str:
    now = datetime.now().astimezone()
    lines = [
        "=" * 60,
        f"  数据更新监测  {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        f"run_id:     {state.get('run_id', '—')}",
        f"状态:       {state.get('status', '—')}",
        f"日志:       {log_path.name}",
        "",
        "已完成步骤:",
    ]
    for s in STEP_ORDER:
        rec = state.get("steps", {}).get(s)
        if rec and rec.get("status") == "ok":
            lines.append(f"  ✓ {s:22} {rec.get('elapsed_s', '?')}s  ({STEP_LABEL.get(s, '')})")
        elif rec and rec.get("status") == "failed":
            lines.append(f"  ✗ {s:22} failed")

    cur = parsed.get("current_step")
    if cur:
        lines.append("")
        lines.append(f"日志当前步: {cur} ({STEP_LABEL.get(cur, '')})")
    if parsed.get("parallel_steps"):
        lines.append(f"  并行: {', '.join(parsed['parallel_steps'])}")

    if parsed["tqdm_by_desc"]:
        lines.append("")
        lines.append("子任务进度 (日志 tqdm):")
        for desc, t in sorted(parsed["tqdm_by_desc"].items(), key=lambda x: -x[1]["cur"]):
            pct = 100.0 * t["cur"] / t["tot"] if t["tot"] else 0
            rem = _fmt_td(t["remain_s"] or 0) if t.get("remain_s") else "?"
            lines.append(f"  {desc}: {t['cur']}/{t['tot']} ({pct:.1f}%)  本步剩余≈{rem}")

    lines.extend(
        [
            "",
            f"整体剩余预估: {_fmt_td(remain_s)}",
            f"预计完成时刻: {(now + timedelta(seconds=remain_s)).strftime('%Y-%m-%d %H:%M:%S') if remain_s > 0 else '—'}",
            f"说明: {note}",
            "",
            "（只读监测；整体 ETA 含未开始步骤的保守预估，越往后越准）",
            "Ctrl+C 退出本窗口，不影响后台更新进程。",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="监测 run_data_update 进度与 ETA")
    parser.add_argument("--log", type=Path, default=None, help="指定日志文件")
    parser.add_argument("--interval", type=float, default=5.0, help="刷新间隔秒")
    args = parser.parse_args()

    log_path = _latest_log(args.log)
    if not log_path:
        print("未找到 data/logs/run_data_update*.log", file=sys.stderr)
        return 1

    try:
        while True:
            state = {}
            if STATE_PATH.is_file():
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            text = _tail_text(log_path)
            parsed = parse_log(text)
            remain_s, note = estimate_remaining_seconds(state, parsed)
            out = render(state, log_path, parsed, remain_s, note)
            if sys.platform == "win32":
                os.system("cls")  # noqa: S605
            else:
                print("\033[2J\033[H", end="")
            print(out)
            if parsed.get("finished") or state.get("status") == "completed":
                print("\n检测到流水线结束，10 秒后退出监测。")
                time.sleep(10)
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
