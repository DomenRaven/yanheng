"""执行 docs/manuals/usability-test-machine.yaml 中的自动化用例。

用法：
  python -m scripts.run_usability_machine_tests
  python -m scripts.run_usability_machine_tests --with-streamlit
  python -m scripts.run_usability_machine_tests --report docs/manuals/usability-test-results-latest.md
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@dataclass
class CaseResult:
    case_id: str
    ok: bool
    detail: str = ""


@dataclass
class RunReport:
    results: list[CaseResult] = field(default_factory=list)

    def add(self, case_id: str, ok: bool, detail: str = "") -> None:
        self.results.append(CaseResult(case_id, ok, detail))

    @property
    def failed(self) -> list[CaseResult]:
        return [r for r in self.results if not r.ok]


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("需要 PyYAML：pip install pyyaml") from None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _run_script(rel: str) -> tuple[int, str]:
    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    exe = str(py) if py.is_file() else sys.executable
    proc = subprocess.run(
        [exe, str(_ROOT / rel.replace("/", os.sep))],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-2000:]


def _run_module(mod: str) -> tuple[int, str]:
    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    exe = str(py) if py.is_file() else sys.executable
    proc = subprocess.run(
        [exe, "-m", mod],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-2000:]


def _assert_python(code: str) -> tuple[bool, str]:
    try:
        exec(compile(code, "<usability-test>", "exec"), {"__name__": "__main__"})
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _fetch(base: str, path: str, forbidden: list[str]) -> tuple[bool, str]:
    if not path.startswith("/"):
        path = "/" + path
    url = base.rstrip("/") + urllib.parse.quote(path, safe="/")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return False, f"无法访问 {url}: {exc}"
    for token in forbidden:
        if token in body:
            return False, f"页面含 {token!r}"
    return True, f"HTTP OK, len={len(body)}"


def _start_streamlit(port: int) -> subprocess.Popen | None:
    py = _ROOT / ".venv" / "Scripts" / "python.exe"
    exe = str(py) if py.is_file() else sys.executable
    log = _ROOT / "data" / "logs" / "usability_streamlit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            exe,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.port",
            str(port),
            "--server.headless",
            "true",
        ],
        cwd=str(_ROOT),
        stdout=f,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        time.sleep(1)
        try:
            urllib.request.urlopen(base, timeout=2)
            return proc
        except OSError:
            continue
    proc.kill()
    return None


def run_spec(spec: dict, *, with_streamlit: bool, port: int) -> RunReport:
    report = RunReport()
    warehouse_ready = _run_module("scripts.check_warehouse_readiness")[0] == 0

    streamlit_proc = None
    if with_streamlit:
        streamlit_proc = _start_streamlit(port)
        if not streamlit_proc:
            report.add("M-S00", False, "Streamlit 启动超时")
        else:
            report.add("M-S00", True, f"Streamlit :{port}")

    try:
        for suite in spec.get("suites", []):
            req = suite.get("requires") or []
            if "warehouse_ready" in req and not warehouse_ready:
                for case in suite.get("cases", []):
                    report.add(case["id"], False, "跳过：warehouse 未就绪")
                continue
            for case in suite.get("cases", []):
                cid = case["id"]
                if "run" in case:
                    code, out = _run_script(case["run"])
                    ok = code == case.get("expect_exit", 0)
                    report.add(cid, ok, out if not ok else "pass")
                elif "run_module" in case:
                    code, out = _run_module(case["run_module"])
                    ok = code == case.get("expect_exit", 0)
                    report.add(cid, ok, out if not ok else "pass")
                elif "assert_python" in case:
                    ok, msg = _assert_python(case["assert_python"])
                    report.add(cid, ok, msg)
                elif "compile_glob" in case:
                    pattern = str(_ROOT / case["compile_glob"])
                    files = glob.glob(pattern)
                    try:
                        for f in files:
                            compile(Path(f).read_text(encoding="utf-8"), f, "exec")
                        report.add(cid, True, f"{len(files)} files")
                    except SyntaxError as exc:
                        report.add(cid, False, str(exc))
                elif "compile_files" in case:
                    ok_all = True
                    for rel in case["compile_files"]:
                        p = _ROOT / rel
                        try:
                            compile(p.read_text(encoding="utf-8"), str(p), "exec")
                        except SyntaxError as exc:
                            ok_all = False
                            report.add(cid, False, str(exc))
                            break
                    else:
                        report.add(cid, True, "syntax ok")
                    continue
                elif "streamlit_url" in case:
                    if not streamlit_proc:
                        if suite.get("optional"):
                            report.add(cid, True, "跳过（未 --with-streamlit）")
                        else:
                            report.add(cid, False, "无 Streamlit")
                        continue
                    path = case["streamlit_url"]
                    base = f"http://127.0.0.1:{port}"
                    ok, msg = _fetch(base, path, case.get("forbid_text") or [])
                    report.add(cid, ok, msg)
                else:
                    report.add(cid, False, "未知用例类型")
    finally:
        if streamlit_proc:
            streamlit_proc.terminate()
            try:
                streamlit_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                streamlit_proc.kill()

    return report


def write_report_md(path: Path, report: RunReport) -> None:
    lines = [
        "# 机器可用性测试结果",
        "",
        f"失败 {len(report.failed)} / 共 {len(report.results)}",
        "",
        "| ID | 结果 | 说明 |",
        "|----|------|------|",
    ]
    for r in report.results:
        lines.append(f"| {r.case_id} | {'PASS' if r.ok else '**FAIL**'} | {r.detail[:120].replace('|', '/')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-streamlit", action="store_true")
    parser.add_argument("--port", type=int, default=8503)
    parser.add_argument(
        "--report",
        default="docs/manuals/usability-test-results-latest.md",
    )
    args = parser.parse_args()

    spec_path = _ROOT / "docs/manuals/usability-test-machine.yaml"
    spec = _load_yaml(spec_path)
    report = run_spec(spec, with_streamlit=args.with_streamlit, port=args.port)
    out = _ROOT / args.report
    write_report_md(out, report)
    print(f"Wrote {out}")
    for r in report.failed:
        print(f"FAIL {r.case_id}: {r.detail[:200]}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
