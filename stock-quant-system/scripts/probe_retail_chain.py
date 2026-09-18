"""散户决策链全流程探针（只读生产库 + 临时库闭环）。

对照 `docs/9.16-散户决策链需求规格.md` §5 / §7。
不跑 run_data_update / run_daily_refresh / generate_daily_advice(生产库)。
"""
from __future__ import annotations

import compileall
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

LOG_PATH = _ROOT / "docs" / "phase5-probe-log-20260918.md"

REGRESSION = (
    "scripts/test_paper_broker.py",
    "scripts/test_advice_sizing.py",
    "scripts/test_entry_rules.py",
    "scripts/test_paper_batch_simulate.py",
    "scripts/test_advice_trace.py",
    "scripts/test_reason_pack.py",
    "scripts/test_industry_cap.py",
    "scripts/test_m8_invalid_if.py",
    "scripts/test_champion_only.py",
    "scripts/demo_retail_chain.py",
    "scripts/audit_phase5_spec_compliance.py",
)

EMPTY_TALK = ("排名大幅下降", "仓位自己把控")


class ProbeLog:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.findings: list[dict] = []
        self.ok = 0
        self.fail = 0
        self.blocked = 0

    def emit(self, status: str, item: str, detail: str = "") -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {status:12} {item}" + (f" — {detail}" if detail else "")
        print(msg, flush=True)
        self.lines.append(msg)
        if status == "OK":
            self.ok += 1
        elif status == "FAIL":
            self.fail += 1
        elif status in ("DATA_BLOCKED", "SKIP"):
            self.blocked += 1
        if status in ("FAIL", "SHALLOW", "DATA_BLOCKED"):
            self.findings.append({"status": status, "item": item, "detail": detail})

    def finding(self, status: str, item: str, detail: str) -> None:
        self.emit(status, item, detail)


def _py() -> str:
    venv = _ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.is_file() else sys.executable


def _run(rel: str) -> tuple[int, str]:
    proc = subprocess.run(
        [_py(), str(_ROOT / rel.replace("/", os.sep))],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out[-2500:]


def probe_code_facts(log: ProbeLog) -> None:
    champ = _ROOT / "mlops" / "registry" / "champion.json"
    if champ.is_file():
        data = json.loads(champ.read_text(encoding="utf-8"))
        run = data.get("run_id")
        exists = (_ROOT / "mlops" / "registry" / str(run) / "model.pkl").is_file()
        if exists:
            log.emit("OK", "M1 champion.json", str(run))
        else:
            log.finding("FAIL", "M1 champion run 目录", f"缺失 {run}")
    else:
        log.finding("FAIL", "M1 champion.json", "文件不存在")

    scanner = (_ROOT / "advice/scanner.py").read_text(encoding="utf-8")
    if "退化为使用最新" in scanner:
        log.finding("FAIL", "M1 scanner 回退", "仍读最新文件夹")
    else:
        log.emit("OK", "M1 scanner 只读冠军")

    engine = (_ROOT / "advice/advice_engine.py").read_text(encoding="utf-8")
    blob = engine
    talk_hits = [t for t in EMPTY_TALK if t in blob]
    if talk_hits:
        log.finding("FAIL", "M8 空话", ",".join(talk_hits))
    else:
        log.emit("OK", "M8 引擎无空话 token")
    if 'action, confidence = "add"' in engine:
        log.finding("FAIL", "M18 add 动作", "引擎仍赋值 add")
    else:
        log.emit("OK", "M18 引擎不赋 add")

    page1 = (_ROOT / "pages/1_持仓与建议.py").read_text(encoding="utf-8")
    if "T+1" in page1:
        log.emit("OK", "M4 UI T+1 说明")
    else:
        log.finding("SHALLOW", "M4 UI T+1", "持仓页无 T+1 文案")

    theme = (_ROOT / "common/ui_theme.py").read_text(encoding="utf-8")
    if "请勿当作胜率" in theme or "分位换算" in theme:
        log.emit("OK", "S4 置信度 caption")
    else:
        log.finding("SHALLOW", "S4 置信度 caption", "卡片未标明分位说明")

    for label, rel in (
        ("M16", "advice/advice_trace.py"),
        ("S1", "advice/reason_pack.py"),
        ("S2", "advice/industry_cap.py"),
        ("M5", "advice/position_sizing.py"),
        ("M11", "advice/entry_rules.py"),
        ("M9", "advice/paper_broker.py"),
    ):
        if (_ROOT / rel).is_file():
            log.emit("OK", f"模块存在 {label}", rel)
        else:
            log.finding("FAIL", f"模块缺失 {label}", rel)


def probe_compile(log: ProbeLog) -> None:
    pages = _ROOT / "pages"
    ok = compileall.compile_dir(str(pages), quiet=1)
    if ok:
        log.emit("OK", "compile pages/")
    else:
        log.finding("FAIL", "compile pages/", "py_compile 失败")
    for rel in ("app.py", "common/ui_theme.py", "advice/advice_engine.py"):
        src = _ROOT / rel
        try:
            compile(src.read_text(encoding="utf-8"), str(src), "exec")
            log.emit("OK", f"compile {rel}")
        except SyntaxError as exc:
            log.finding("FAIL", f"compile {rel}", str(exc))


def probe_warehouse(log: ProbeLog) -> None:
    from common.db import get_ui_connection, init_schema
    from common.warehouse_readiness import assess_warehouse_readiness

    try:
        conn = get_ui_connection()
        init_schema(conn)
        rep = assess_warehouse_readiness(conn)
        n_advice = conn.execute("SELECT COUNT(*) FROM advice_log").fetchone()[0]
        n_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        from advice.advice_trace import complete_trace_count, load_advice_traces

        traces = load_advice_traces(conn)
        n_trace_rows = len(traces)
        n_complete = complete_trace_count(traces)
        conn.close()
        metrics = {k: rep.metrics.get(k) for k in (
            "universe_active",
            "effective_quote_date",
            "expected_latest_trade_date",
            "quote_coverage_ratio",
            "limit_price_coverage",
            "daily_basic_coverage",
        )}
        detail = (
            f"ready={rep.ready} advice_log={n_advice} paper_trades={n_trades} "
            f"trace_rows={n_trace_rows} complete_traces={n_complete} {metrics}"
        )
        if n_trades == 0:
            log.emit(
                "OK",
                "M16 生产成交条数",
                "paper_trades=0，完整 trace=0（用户尚未模拟/M15；非字段缺失）",
            )
        if rep.ready:
            log.emit("OK", "生产库就绪", detail)
        else:
            blockers = "; ".join(rep.blockers[:6])
            log.finding("DATA_BLOCKED", "生产库未就绪", f"{detail}; blockers={blockers}")
            log.emit(
                "SKIP",
                "生产 generate_daily_advice / scanner",
                "灌库暂停且未就绪，禁止写生产库",
            )
    except Exception as exc:
        log.finding("DATA_BLOCKED", "生产库打开失败", str(exc))


def probe_regressions(log: ProbeLog) -> None:
    for rel in REGRESSION:
        code, out = _run(rel)
        last = out.splitlines()[-1] if out else ""
        if code == 0:
            log.emit("OK", rel, last[:180])
        else:
            log.finding("FAIL", rel, last[:400] or f"exit {code}")
            if out:
                print(out[-1500:], flush=True)


def write_markdown(log: ProbeLog) -> None:
    body = [
        "# Phase 5 全流程探针日志（2026-09-18）",
        "",
        "> 对照 `9.16-散户决策链需求规格.md` §5 / §7。生产灌库保持暂停。",
        "",
        f"- 结束时间：{dt.datetime.now().isoformat(timespec='seconds')}",
        f"- OK={log.ok}  FAIL={log.fail}  DATA_BLOCKED/SKIP={log.blocked}",
        "",
        "## 闸门 1.5 测试理论",
        "",
        "- 证据包：CD4ML / 本仓库八闸门第 4 步——「能跑不报错」不算过；Must 用临时库数字 + 源码事实。",
        "- 日频执行缺口：`docs/03-量化方法/15` + Perold IS 简化（`04-风险管理/11`），trace 用 fill vs 信号收盘。",
        "- 生产库未就绪记 **DATA_BLOCKED**，不伪装成代码未实现。",
        "",
        "## 实时探针输出",
        "",
        "```",
        *log.lines,
        "```",
        "",
        "## 探明的错误 / 缺漏 / 浅实现",
        "",
    ]
    if not log.findings:
        body.append("本轮探针未发现代码 FAIL；数据阻塞见上。")
    else:
        body.append("| 级别 | 项 | 细节 |")
        body.append("|------|----|------|")
        for f in log.findings:
            detail = str(f["detail"]).replace("|", "\\|")
            body.append(f"| {f['status']} | {f['item']} | {detail} |")
    body.extend(["", "*由 `scripts/probe_retail_chain.py` 生成。*", ""])
    LOG_PATH.write_text("\n".join(body), encoding="utf-8")
    print(f"WROTE {LOG_PATH}", flush=True)


def main() -> int:
    log = ProbeLog()
    probe_code_facts(log)
    probe_compile(log)
    probe_warehouse(log)
    probe_regressions(log)
    write_markdown(log)
    print(
        f"SUMMARY ok={log.ok} fail={log.fail} blocked={log.blocked}",
        flush=True,
    )
    return 1 if log.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
