"""无人值守链路 + 影子仓 + UI 入口全流程探针（真实生产库）。

对照：
- docs/shadow-farm-notes.md
- docs/data-maintenance-policy.md（周末链路）
- 用户诉求：周末灌库成功 → 双池建议 → 影子仓，无需点 UI

本探针会写生产 advice_log / shadow_*（与计划任务同路径），不做全市场 weekend_research 灌库
（耗时长；灌库能力用 PROFILE + 断点状态 + 任务注册校验）。

输出：docs/probe-unsupervised-chain-YYYYMMDD.md
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

LOG_PATH = _ROOT / "docs" / f"probe-unsupervised-chain-{dt.date.today().isoformat().replace('-', '')}.md"

REGRESSION = (
    "scripts/test_shadow_farm.py",
    "scripts/test_paper_broker.py",
    "scripts/test_champion_only.py",
)

UI_PAGES = (
    "app.py",
    "pages/1_持仓与建议.py",
    "pages/2_掘金扫描.py",
    "pages/8_明日待办.py",
    "pages/9_影子仓体检.py",
    "pages/7_历史建议复盘.py",
)


class ProbeLog:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.findings: list[dict] = []
        self.ok = 0
        self.fail = 0
        self.warn = 0
        self.skip = 0

    def emit(self, status: str, item: str, detail: str = "") -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {status:12} {item}" + (f" — {detail}" if detail else "")
        print(msg, flush=True)
        self.lines.append(msg)
        if status == "OK":
            self.ok += 1
        elif status == "FAIL":
            self.fail += 1
            self.findings.append({"status": status, "item": item, "detail": detail})
        elif status == "WARN":
            self.warn += 1
            self.findings.append({"status": status, "item": item, "detail": detail})
        elif status in ("SKIP", "DATA_BLOCKED"):
            self.skip += 1
            self.findings.append({"status": status, "item": item, "detail": detail})

    def write(self) -> None:
        body = [
            f"# 无人值守链路探针 {dt.date.today().isoformat()}",
            "",
            "> 真实库证据；短窗影子盈亏不进入晋升。",
            "",
            f"- OK={self.ok} FAIL={self.fail} WARN={self.warn} SKIP={self.skip}",
            "",
            "## 运行日志",
            "",
            "```",
            *self.lines,
            "```",
            "",
            "## 待修复清单",
            "",
        ]
        if not self.findings:
            body.append("_无 FAIL/WARN/DATA_BLOCKED。_")
        else:
            for f in self.findings:
                body.append(f"- **{f['status']}** `{f['item']}` — {f['detail']}")
        body.append("")
        LOG_PATH.write_text("\n".join(body), encoding="utf-8")
        print(f"\nWrote {LOG_PATH}", flush=True)


def _home_py() -> str:
    """与计划任务一致：pyvenv home + site-packages，避免 Windows venv 双进程抢锁。"""
    cfg = _ROOT / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        return sys.executable
    home = None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("home"):
            home = line.split("=", 1)[1].strip()
            break
    if not home:
        return sys.executable
    py = Path(home) / "python.exe"
    return str(py) if py.is_file() else sys.executable


def _env() -> dict[str, str]:
    env = os.environ.copy()
    site = _ROOT / ".venv" / "Lib" / "site-packages"
    env["VIRTUAL_ENV"] = str(_ROOT / ".venv")
    env["PYTHONPATH"] = str(site)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _run_mod(mod: str, *extra: str, timeout: int = 600) -> tuple[int, str]:
    cmd = [_home_py(), "-u", "-m", mod, *extra]
    proc = subprocess.run(
        cmd,
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        timeout=timeout,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out[-4000:]


def probe_static(log: ProbeLog) -> None:
    from scripts.run_daily_refresh import PROFILE_ARGV

    chain = _ROOT / "scripts" / "run_weekend_shadow_chain.py"
    inv = _ROOT / "scripts" / "invoke_scheduled_weekend_shadow_chain.ps1"
    if chain.is_file() and inv.is_file():
        log.emit("OK", "链路脚本存在", f"{chain.name} + {inv.name}")
    else:
        log.emit("FAIL", "链路脚本缺失", f"py={chain.is_file()} ps1={inv.is_file()}")

    wr = PROFILE_ARGV.get("weekend_research")
    if wr and "quotes" in ",".join(wr):
        log.emit("OK", "weekend_research profile", ",".join(wr[1].split(",")[:4]) + ",…")
    else:
        log.emit("FAIL", "weekend_research profile", str(wr))

    for pool in ("hs", "bj"):
        p = _ROOT / "mlops" / "registry" / f"champion_{pool}.json"
        if not p.is_file():
            log.emit("FAIL", f"champion_{pool}", "缺失")
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        run = data.get("run_id")
        model = _ROOT / "mlops" / "registry" / str(run) / "model.pkl"
        if model.is_file():
            log.emit("OK", f"champion_{pool}", str(run))
        else:
            log.emit("FAIL", f"champion_{pool} model", str(run))

    for rel in UI_PAGES:
        path = _ROOT / rel
        if not path.is_file():
            log.emit("FAIL", f"UI 文件 {rel}", "缺失")
            continue
        text = path.read_text(encoding="utf-8")
        if rel.endswith("9_影子仓体检.py"):
            need = ("run_shadow_farm_once", "导出 CSV", "用 AI 解读", "ret_")
            missing = [n for n in need if n not in text]
            if missing:
                log.emit("FAIL", "影子仓页关键文案", str(missing))
            else:
                log.emit("OK", "影子仓页关键文案")
        log.emit("OK", f"UI 文件可读 {rel}")


def probe_tasks(log: ProbeLog) -> None:
    ps = (
        "Get-ScheduledTask -TaskName 'StockQuant-WeekendResearch','StockQuant-ShadowFarm' "
        "| ForEach-Object { $a=$_.Actions; "
        "\"$($_.TaskName)|$($_.State)|$($a.Arguments)\" }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        log.emit("FAIL", "计划任务查询", (proc.stderr or "")[-500:])
        return
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name, state, args = parts[0], parts[1], parts[2]
        if name == "StockQuant-WeekendResearch":
            if "weekend_shadow_chain" in args and state == "Ready":
                log.emit("OK", name, f"{state} → chain")
            else:
                log.emit("FAIL", name, f"{state} args={args[-120:]}")
        elif name == "StockQuant-ShadowFarm":
            if "shadow_farm" in args and state == "Ready":
                log.emit("OK", name, state)
            else:
                log.emit("FAIL", name, f"{state} args={args[-120:]}")


def probe_warehouse(log: ProbeLog) -> None:
    from common.db import get_connection, init_schema
    from common.warehouse_readiness import assess_warehouse_readiness, ingestion_update_in_progress
    from common.ingestion_engine.pipeline_state import load_state

    if ingestion_update_in_progress():
        st = load_state() or {}
        log.emit(
            "WARN",
            "灌库断点仍在进行中",
            f"run_id={st.get('run_id')} status={st.get('status')} steps={len(st.get('steps') or {})}",
        )
    else:
        log.emit("OK", "无进行中灌库锁")

    conn = get_connection()
    try:
        init_schema(conn)
        report = assess_warehouse_readiness(conn)
        log.emit(
            "OK" if report.ready else "DATA_BLOCKED",
            "仓库投产就绪",
            f"ready={report.ready} blockers={report.blockers[:3]} "
            f"eff={report.metrics.get('effective_quote_date')} "
            f"exp={report.metrics.get('expected_latest_trade_date')}",
        )
        for pool in ("hs", "bj"):
            row = conn.execute(
                """
                SELECT as_of, count(*) AS n FROM advice_log
                WHERE coalesce(pool_id, 'hs') = ?
                GROUP BY as_of ORDER BY as_of DESC LIMIT 1
                """,
                [pool],
            ).fetchone()
            if row:
                log.emit("OK", f"advice_log[{pool}]", f"as_of={row[0]} n={row[1]}")
            else:
                log.emit("WARN", f"advice_log[{pool}]", "无记录")

        nav_n = conn.execute("SELECT count(*) FROM shadow_nav_daily").fetchone()[0]
        run_n = conn.execute("SELECT count(*) FROM shadow_run_log").fetchone()[0]
        log.emit("OK", "shadow 表", f"nav_rows={nav_n} run_rows={run_n}")

        acc = conn.execute(
            "SELECT count(*) FROM paper_account WHERE coalesce(kind,'human') = 'shadow'"
        ).fetchone()[0]
        if acc >= 8:
            log.emit("OK", "影子账户数", str(acc))
        else:
            log.emit("WARN", "影子账户数不足 8", str(acc))
    finally:
        conn.close()


def probe_regressions(log: ProbeLog) -> None:
    for rel in REGRESSION:
        proc = subprocess.run(
            [_home_py(), str(_ROOT / rel.replace("/", os.sep))],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            timeout=180,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            log.emit("OK", f"回归 {rel}", out.splitlines()[-1][:120] if out else "exit=0")
        else:
            log.emit("FAIL", f"回归 {rel}", out[-800:])


def probe_chain_live(log: ProbeLog) -> None:
    """跳过灌库：双池建议 + 影子仓（真实写库）。"""
    code, out = _run_mod(
        "scripts.run_weekend_shadow_chain",
        "--skip-ingest",
        timeout=600,
    )
    report_path = _ROOT / "data" / "shadow_farm" / "latest_weekend_chain.json"
    payload = None
    if report_path.is_file():
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.emit("WARN", "latest_weekend_chain.json 损坏", str(exc))

    if payload is None:
        marker = "===WEEKEND_SHADOW_CHAIN_REPORT==="
        if marker in out:
            chunk = out.split(marker, 1)[1].strip()
            if "===REPORT_FILE=" in chunk:
                chunk = chunk.split("===REPORT_FILE=", 1)[0].strip()
            try:
                payload = json.loads(chunk)
            except json.JSONDecodeError:
                payload = None

    if code != 0 and not payload:
        log.emit("FAIL", "周末链路 --skip-ingest", out[-800:])
        return

    if not payload:
        log.emit("FAIL", "周末链路 JSON 解析", out[-500:])
        return

    adv = payload.get("advice") or {}
    if adv.get("status") == "ok":
        pools = adv.get("pools") or {}
        for pid in ("hs", "bj"):
            p = pools.get(pid) or {}
            if p.get("status") == "ok":
                log.emit(
                    "OK",
                    f"链路建议 {pid}",
                    f"as_of={p.get('as_of')} todos={p.get('n_todos')} watch={p.get('n_watchlist')}",
                )
            else:
                log.emit("FAIL", f"链路建议 {pid}", str(p)[:200])
    else:
        log.emit("FAIL", "链路建议总状态", str(adv)[:200])

    sh = payload.get("shadow") or {}
    if sh.get("status") in ("ok", "skipped_data"):
        log.emit("OK", "链路影子仓", f"status={sh.get('status')} as_of={sh.get('as_of')} n={sh.get('n_strategies')}")
    else:
        log.emit("FAIL", "链路影子仓", str(sh)[:200])

    # 截断捕获时仍可读文件
    report_path = _ROOT / "data" / "shadow_farm" / "latest_weekend_chain.json"
    if report_path.is_file():
        mtime = dt.datetime.fromtimestamp(report_path.stat().st_mtime)
        age_s = (dt.datetime.now() - mtime).total_seconds()
        if age_s < 600:
            log.emit("OK", "latest_weekend_chain.json", f"age_s={age_s:.0f}")
        else:
            log.emit("WARN", "latest_weekend_chain.json 偏旧", f"age_s={age_s:.0f}")

    if payload.get("exit_code", code) not in (0,):
        log.emit("WARN", "链路 exit_code", str(payload.get("exit_code")))


def probe_shadow_report_file(log: ProbeLog) -> None:
    latest = _ROOT / "data" / "shadow_farm" / "latest_report.json"
    if not latest.is_file():
        log.emit("WARN", "latest_report.json", "缺失")
        return
    data = json.loads(latest.read_text(encoding="utf-8"))
    strats = data.get("strategies") or []
    if len(strats) >= 8:
        log.emit("OK", "latest_report.json", f"status={data.get('status')} n={len(strats)} as_of={data.get('as_of')}")
    else:
        log.emit("FAIL", "latest_report.json 策略数", str(len(strats)))
    # horizons 20/30
    hz_keys = set()
    for s in strats:
        hz_keys.update((s.get("horizons") or {}).keys())
    for need in ("ret_20d", "ret_30d"):
        if need in hz_keys:
            log.emit("OK", f"horizon {need}")
        else:
            log.emit("WARN", f"horizon {need}", f"keys={sorted(hz_keys)}")


def probe_stale_clear(log: ProbeLog) -> None:
    from scripts.run_weekend_shadow_chain import _clear_stale_pipeline_lock, _ingest_ok

    assert _ingest_ok({"a": {"status": "ok"}, "_meta": {}})
    assert not _ingest_ok({"a": {"status": "failed"}})
    log.emit("OK", "_ingest_ok 单测逻辑")
    # 不应在无锁时爆炸
    _clear_stale_pipeline_lock(max_age_hours=2.0)
    log.emit("OK", "_clear_stale_pipeline_lock 可调用")


def main() -> int:
    log = ProbeLog()
    log.emit("OK", "探针启动", f"root={_ROOT}")
    try:
        probe_static(log)
        probe_tasks(log)
        probe_stale_clear(log)
        probe_warehouse(log)
        probe_regressions(log)
        probe_chain_live(log)
        probe_shadow_report_file(log)
    except Exception as exc:
        log.emit("FAIL", "探针未捕获异常", f"{type(exc).__name__}: {exc}")
    log.write()
    return 1 if log.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
