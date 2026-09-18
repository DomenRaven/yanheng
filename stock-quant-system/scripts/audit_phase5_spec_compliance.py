"""对照 `docs/9.16-散户决策链需求规格.md` §5 Must 与落盘 schema 做机械自查。

不连生产 warehouse（除非环境变量 STOCK_QUANT_DB 已设）；默认用临时库验证 DDL。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import duckdb

from common.db import init_schema

ADVICE_LOG_PHASE5_COLS = (
    "size_shares",
    "est_amount_cny",
    "size_pct_nav",
    "max_loss_cny",
    "horizon_days",
    "exec_date",
)

PAPER_TABLES = (
    "paper_account",
    "paper_cash",
    "paper_positions",
    "paper_trades",
)

MODULE_PATHS = {
    "M1 scanner": "advice/scanner.py",
    "M4/M9 broker": "advice/paper_broker.py",
    "M5 sizing": "advice/position_sizing.py",
    "M11 entry": "advice/entry_rules.py",
    "M2 engine": "advice/advice_engine.py",
    "champion": "mlops/registry/champion.json",
    "M11 UI": "pages/8_明日待办.py",
    "M15 UI": "pages/7_历史建议复盘.py",
    "M16 trace": "advice/advice_trace.py",
    "S1 reason pack": "advice/reason_pack.py",
    "S2 industry cap": "advice/industry_cap.py",
    "M17 footer": "common/ui_theme.py",
}

TEST_SCRIPTS = (
    "scripts/test_paper_broker.py",
    "scripts/test_advice_sizing.py",
    "scripts/test_entry_rules.py",
    "scripts/test_paper_batch_simulate.py",
    "scripts/test_advice_trace.py",
    "scripts/test_reason_pack.py",
    "scripts/test_industry_cap.py",
    "scripts/test_m8_invalid_if.py",
    "scripts/test_champion_only.py",
)

EMPTY_TALK_TOKENS = ("排名大幅下降", "仓位自己把控")


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}


def audit_schema(conn) -> list[str]:
    issues: list[str] = []
    init_schema(conn)
    for t in PAPER_TABLES:
        cols = _columns(conn, t)
        if not cols:
            issues.append(f"缺少表 {t}")
    adv = _columns(conn, "advice_log")
    for c in ADVICE_LOG_PHASE5_COLS:
        if c not in adv:
            issues.append(f"advice_log 缺少列 {c}")
    if "reason_one_liner" not in adv:
        issues.append("advice_log 缺少列 reason_one_liner（S1 跨会话回放）")
    return issues


def audit_files() -> list[str]:
    issues: list[str] = []
    for label, rel in MODULE_PATHS.items():
        if not (_ROOT / rel).is_file():
            issues.append(f"缺少文件 [{label}]: {rel}")
    for rel in TEST_SCRIPTS:
        if not (_ROOT / rel).is_file():
            issues.append(f"缺少回归脚本: {rel}")
    engine = (_ROOT / "advice/advice_engine.py").read_text(encoding="utf-8")
    if 'action, confidence = "add"' in engine or "action = \"add\"" in engine:
        issues.append("advice_engine 仍会赋 action=add（M18 风险）")
    for tok in EMPTY_TALK_TOKENS:
        if tok in engine:
            issues.append(f"advice_engine 仍含空话 {tok!r}（M8）")
    scanner = (_ROOT / "advice/scanner.py").read_text(encoding="utf-8")
    if "退化为使用最新" in scanner:
        issues.append("scanner 仍回退最新训练文件夹（M1）")
    page1 = (_ROOT / "pages/1_持仓与建议.py").read_text(encoding="utf-8")
    if "T+1" not in page1:
        issues.append("持仓页缺少 T+1 UI 说明（M4）")
    theme = (_ROOT / "common/ui_theme.py").read_text(encoding="utf-8")
    if "请勿当作胜率" not in theme and "分位换算" not in theme:
        issues.append("卡片缺少置信度分位说明（S4）")
    return issues


def audit_production_db() -> list[str]:
    notes: list[str] = []
    from common.db import get_db_path, get_ui_connection, init_schema

    path = get_db_path()
    if not path.is_file():
        notes.append(f"生产库不存在 {path}")
        return notes
    try:
        conn = get_ui_connection()
        init_schema(conn)
        end = conn.execute(
            "SELECT MAX(trade_date) FROM daily_quotes WHERE adjust='qfq'"
        ).fetchone()
        notes.append(f"生产库 daily_quotes 截止: {end[0] if end and end[0] else '—'}")
        for t in ("advice_log", "paper_account", "paper_trades"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                notes.append(f"生产库 {t} 行数: {n}")
            except Exception:
                notes.append(f"生产库无表或不可读: {t}")
        conn.close()
    except Exception as e:
        notes.append(f"生产库打开失败: {e}")
    return notes


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Phase 5 规格机械自查")
    parser.add_argument(
        "--include-warehouse",
        action="store_true",
        help="把生产库未就绪也记为失败（默认只记笔记，不与代码/schema 混成同一退出码）",
    )
    args = parser.parse_args()

    issues = audit_files()
    prev_db = os.environ.get("STOCK_QUANT_DB")
    path = Path(tempfile.mkdtemp()) / "audit_phase5.duckdb"
    os.environ["STOCK_QUANT_DB"] = str(path)
    conn = duckdb.connect(str(path))
    try:
        issues.extend(audit_schema(conn))
    finally:
        conn.close()
        if prev_db is None:
            os.environ.pop("STOCK_QUANT_DB", None)
        else:
            os.environ["STOCK_QUANT_DB"] = prev_db

    print("=== Phase5 spec compliance audit ===")
    if issues:
        print("FAIL (code/schema):")
        for i in issues:
            print(" ", i)
    else:
        print("Schema + file checks: OK")

    print("\nProduction notes (data, not Must-code):")
    for n in audit_production_db():
        print(" ", n)

    warehouse_blocked = False
    db_path = os.environ.get("STOCK_QUANT_DB")
    if not db_path:
        from common.config import get_config
        from common.db import resolve_path

        db_path = str(resolve_path(get_config()["storage"]["duckdb_path"]))
    if Path(db_path).is_file():
        from common.db import get_ui_connection, init_schema
        from common.warehouse_readiness import assess_warehouse_readiness

        try:
            conn = get_ui_connection()
            init_schema(conn)
            rep = assess_warehouse_readiness(conn)
            conn.close()
            print("  production_ready:", rep.ready)
            if not rep.ready:
                warehouse_blocked = True
                for b in rep.blockers:
                    print("   blocker:", b)
        except Exception as exc:
            warehouse_blocked = True
            print(f"  production_ready check failed: {exc}")

    if warehouse_blocked:
        print("DATA_BLOCKED: 生产库未达投产门槛（规格 §12.4；不冒充 Must 代码失败）")

    if issues:
        return 1
    if args.include_warehouse and warehouse_blocked:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
