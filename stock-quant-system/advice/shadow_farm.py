"""机器影子仓农场：少量固定策略并行记账，供多日体检。

对照 docs/shadow-farm-notes.md：短窗盈亏只做监控，不进入冠军训练/晋升。
成交规则复用 paper_broker（次日开盘 + 滑点 + T+1 等）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from advice.advice_engine import load_latest_advice_cards, parse_as_of_date
from advice.entry_rules import build_tomorrow_todos
from advice.paper_broker import (
    create_paper_account,
    get_cash,
    last_quote_date,
    mark_to_market_nav,
    simulate_advice_cards,
)
from common.db import get_connection, init_schema, write_session

logger = logging.getLogger("advice.shadow_farm")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_ROOT / "data" / "shadow_farm"
# 相对「已有净值快照」回溯的第 N 个交易日收益；20/30 与训练标签量级对齐作旁证
HORIZONS = (1, 3, 7, 15, 20, 30)
TEMPLATE_ID = "live_prep_10k"
# 账户一经建立至少留存天数；期满后由 prune 清理账户行（磁盘监测归档仍保留便于复盘）
RETENTION_DAYS = 60
ACCOUNTS_DIR = REPORT_DIR / "accounts"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    pool_id: str
    mode: str  # todos | top_n
    top_n: int | None = None
    deploy_pct: float = 1.0
    label: str = ""


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("hs_todos", "hs", "todos", label="沪深·明日待办原文"),
    StrategySpec("hs_top5", "hs", "top_n", top_n=5, deploy_pct=1.0, label="沪深·前5等权满仓"),
    StrategySpec("hs_top10", "hs", "top_n", top_n=10, deploy_pct=1.0, label="沪深·前10等权满仓"),
    StrategySpec("hs_top20", "hs", "top_n", top_n=20, deploy_pct=1.0, label="沪深·前20等权满仓"),
    StrategySpec("hs_top10_half", "hs", "top_n", top_n=10, deploy_pct=0.5, label="沪深·前10等权半仓"),
    StrategySpec("bj_todos", "bj", "todos", label="北交所·明日待办原文"),
    StrategySpec("bj_top5", "bj", "top_n", top_n=5, deploy_pct=1.0, label="北交所·前5等权满仓"),
    StrategySpec("bj_top10", "bj", "top_n", top_n=10, deploy_pct=1.0, label="北交所·前10等权满仓"),
)

# 产品约定同时活跃上限：8 策略 × floor(60/7) 周 = 64（ISO 周边界偶发可略高，仍按创建日+60天 prune）
MAX_CONCURRENT_ACCOUNTS = len(STRATEGY_SPECS) * (RETENTION_DAYS // 7)


def cohort_id_for_date(d: dt.date) -> str:
    """ISO 周编号，如 2026-W38（周一为一周之始）。"""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _spec_by_id(strategy_id: str) -> StrategySpec:
    for s in STRATEGY_SPECS:
        if s.strategy_id == strategy_id:
            return s
    raise KeyError(strategy_id)


def _tag_legacy_shadow_cohorts(conn) -> None:
    """旧版固定账户（无 cohort / 无周 slug）一律打上 legacy，不并入本周队列。"""
    rows = conn.execute(
        """
        SELECT account_id FROM paper_account
        WHERE coalesce(kind, 'human') = 'shadow'
          AND (cohort_id IS NULL OR cohort_id = '')
        """
    ).fetchall()
    for (aid,) in rows:
        conn.execute(
            "UPDATE paper_account SET cohort_id = 'legacy' WHERE account_id = ?",
            [aid],
        )


def _dedupe_shadow_cohort(conn, cohort_id: str) -> None:
    """同一周+同一策略若有多户（旧固定户误入周队列），只保留最新一户，其余改标 legacy。"""
    if not cohort_id or cohort_id == "legacy":
        return
    for spec in STRATEGY_SPECS:
        rows = conn.execute(
            """
            SELECT account_id FROM paper_account
            WHERE coalesce(kind, 'human') = 'shadow'
              AND strategy_id = ?
              AND cohort_id = ?
            ORDER BY created_at DESC NULLS LAST, account_id DESC
            """,
            [spec.strategy_id, cohort_id],
        ).fetchall()
        if len(rows) <= 1:
            continue
        for (aid,) in rows[1:]:
            conn.execute(
                "UPDATE paper_account SET cohort_id = 'legacy' WHERE account_id = ?",
                [aid],
            )
            logger.info(
                "影子仓去重：%s 从 %s 改标 legacy（保留更新户）",
                aid,
                cohort_id,
            )


def ensure_shadow_cohort(cohort_id: str) -> dict[str, str]:
    """幂等：为指定周队列确保 8 个影子账户。返回 {strategy_id: account_id}。"""
    out: dict[str, str] = {}
    with write_session(init=True) as conn:
        _tag_legacy_shadow_cohorts(conn)
        _dedupe_shadow_cohort(conn, cohort_id)
        for spec in STRATEGY_SPECS:
            row = conn.execute(
                """
                SELECT account_id FROM paper_account
                WHERE coalesce(kind, 'human') = 'shadow'
                  AND strategy_id = ?
                  AND cohort_id = ?
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """,
                [spec.strategy_id, cohort_id],
            ).fetchone()
            if row:
                out[spec.strategy_id] = row[0]
    for spec in STRATEGY_SPECS:
        if spec.strategy_id in out:
            continue
        aid = create_paper_account(
            TEMPLATE_ID,
            note=f"shadow:{cohort_id}:{spec.strategy_id}",
            kind="shadow",
            strategy_id=spec.strategy_id,
            cohort_id=cohort_id,
        )
        out[spec.strategy_id] = aid
    return out


def ensure_shadow_accounts() -> dict[str, str]:
    """兼容旧调用：确保「本周」队列 8 户存在。"""
    return ensure_shadow_cohort(cohort_id_for_date(dt.date.today()))


def list_active_shadow_accounts(
    conn,
    *,
    as_of: dt.date,
    retention_days: int = RETENTION_DAYS,
) -> list[dict[str, Any]]:
    """未满 retention_days 的影子账户（含本周新建与往周仍在留存期内的）。"""
    cutoff = as_of - dt.timedelta(days=int(retention_days))
    rows = conn.execute(
        """
        SELECT account_id, strategy_id, cohort_id, created_at, note
        FROM paper_account
        WHERE coalesce(kind, 'human') = 'shadow'
          AND cast(created_at AS DATE) >= ?
        ORDER BY cohort_id DESC NULLS LAST, strategy_id
        """,
        [cutoff],
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "account_id": r[0],
                "strategy_id": r[1],
                "cohort_id": r[2] or "legacy",
                "created_at": r[3],
                "note": r[4],
            }
        )
    return out


def prune_expired_shadow_accounts(
    conn,
    *,
    as_of: dt.date,
    retention_days: int = RETENTION_DAYS,
) -> int:
    """删除创建超过 retention_days 的影子账户及其现金/持仓/成交。

    shadow_nav_daily 快照与 data/shadow_farm/accounts/ 磁盘归档保留，便于复盘。
    """
    cutoff = as_of - dt.timedelta(days=int(retention_days))
    old = conn.execute(
        """
        SELECT account_id FROM paper_account
        WHERE coalesce(kind, 'human') = 'shadow'
          AND cast(created_at AS DATE) < ?
        """,
        [cutoff],
    ).fetchall()
    n = 0
    for (aid,) in old:
        conn.execute("DELETE FROM paper_trades WHERE account_id = ?", [aid])
        conn.execute("DELETE FROM paper_positions WHERE account_id = ?", [aid])
        conn.execute("DELETE FROM paper_cash WHERE account_id = ?", [aid])
        conn.execute("DELETE FROM paper_account WHERE account_id = ?", [aid])
        n += 1
    if n:
        logger.info("已清理过期影子账户 %d 个（早于 %s）", n, cutoff)
    return n


def _card_rank(card: dict) -> float:
    for reason in card.get("reasons") or []:
        if reason.get("type") == "model" and "排名第" in str(reason.get("detail") or ""):
            try:
                tail = str(reason["detail"]).split("排名第", 1)[1]
                return float(tail.split("名")[0])
            except (IndexError, ValueError):
                pass
    conf = card.get("confidence")
    try:
        return 1e6 - float(conf or 0) * 1000
    except (TypeError, ValueError):
        return 1e6


def _ref_price(conn, symbol: str, as_of: dt.date) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM daily_quotes
        WHERE symbol = ? AND trade_date <= ? AND adjust = 'qfq'
        ORDER BY trade_date DESC LIMIT 1
        """,
        [symbol, as_of],
    ).fetchone()
    if row and row[0] is not None and float(row[0]) > 0:
        return float(row[0])
    return None


def cards_for_strategy(
    conn,
    cards: list[dict],
    spec: StrategySpec,
    *,
    signal_date: dt.date,
    account_id: str,
    quote_asof: dt.date,
) -> list[dict]:
    """按策略改写可模拟卡片（股数 100 整手）。

    advice_id 一律加 shadow 前缀，避免与人手练习仓或其它策略共用同一 id 被幂等跳过。
    top_n 采用「按排名贪心买满」：半仓等场景下等权拆太碎会全买不进 1 手。
    """
    def _shadow_aid(raw: object, symbol: str) -> str:
        base = str(raw).strip() if raw else symbol
        return f"shadow:{spec.strategy_id}:{base}:{quote_asof.isoformat()}"

    if spec.mode == "todos":
        allow_bj = spec.pool_id == "bj"
        todos = build_tomorrow_todos(
            cards, signal_date, conn, max_items=3, allow_bj_open=allow_bj
        )
        out: list[dict] = []
        for t in todos:
            out.append(
                {
                    "advice_id": _shadow_aid(t.get("advice_id"), str(t["symbol"])),
                    "symbol": t["symbol"],
                    "action": t["action"],
                    "size_shares": int(t.get("size_shares") or 0),
                    "exec_date": t.get("exec_date"),
                }
            )
        return out

    opens = [c for c in cards if c.get("action") == "open"]
    opens.sort(key=_card_rank)
    n = int(spec.top_n or 5)
    picked = opens[:n]
    if not picked:
        return []

    # deploy_pct：目标「市值 / 净值」上限，而非对剩余现金再乘系数
    # （半仓账户已接近目标后应空转盯市，而不是用所剩现金的 50% 永远买不进 1 手）
    nav = mark_to_market_nav(conn, account_id, as_of=quote_asof)
    cash = float(nav.get("cash_cny") or 0.0)
    mv = float(nav.get("market_value_cny") or 0.0)
    total = float(nav.get("nav_cny") or 0.0) or (cash + mv)
    target_mv = total * float(spec.deploy_pct)
    budget = max(0.0, min(cash, target_mv - mv))

    def _pack(c: dict, shares: int) -> dict:
        return {
            "advice_id": _shadow_aid(c.get("advice_id"), str(c["symbol"])),
            "symbol": c["symbol"],
            "action": "open",
            "size_shares": shares,
            "exec_date": c.get("exec_date"),
        }

    # 先尝试等权；半仓+多票时人均预算常不足 1 手 → 回退为按排名贪心
    per = budget / len(picked)
    equal_out: list[dict] = []
    for c in picked:
        px = _ref_price(conn, str(c["symbol"]), quote_asof)
        if px is None or px <= 0:
            continue
        raw_shares = int(per / px / 100) * 100
        if raw_shares >= 100:
            equal_out.append(_pack(c, raw_shares))
    if equal_out:
        return equal_out

    remaining = budget
    greedy_out: list[dict] = []
    for c in picked:
        if remaining < 1.0:
            break
        px = _ref_price(conn, str(c["symbol"]), quote_asof)
        if px is None or px <= 0:
            continue
        max_lots = int(remaining / (px * 100))
        if max_lots < 1:
            continue
        raw_shares = max_lots * 100
        remaining -= raw_shares * px
        greedy_out.append(_pack(c, raw_shares))
    return greedy_out


def _upsert_nav(
    conn,
    account_id: str,
    strategy_id: str,
    as_of: dt.date,
    nav: dict[str, float],
    *,
    cohort_id: str | None = None,
    filled: int = 0,
    pending: int = 0,
    rejected: int = 0,
    skipped: int = 0,
) -> None:
    conn.execute(
        "DELETE FROM shadow_nav_daily WHERE account_id = ? AND as_of = ?",
        [account_id, as_of],
    )
    conn.execute(
        """
        INSERT INTO shadow_nav_daily
          (account_id, cohort_id, strategy_id, as_of, cash_cny, market_value_cny, nav_cny,
           filled, pending, rejected, skipped)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            account_id,
            cohort_id,
            strategy_id,
            as_of,
            float(nav["cash_cny"]),
            float(nav["market_value_cny"]),
            float(nav["nav_cny"]),
            int(filled),
            int(pending),
            int(rejected),
            int(skipped),
        ],
    )


def _horizon_returns(conn, account_id: str, as_of: dt.date) -> dict[str, float | None]:
    """相对 as_of，回溯第 N 个已有快照交易日的净值变化（按账户）。"""
    hist = conn.execute(
        """
        SELECT as_of, nav_cny FROM shadow_nav_daily
        WHERE account_id = ? AND as_of <= ?
        ORDER BY as_of DESC
        """,
        [account_id, as_of],
    ).df()
    out: dict[str, float | None] = {f"ret_{h}d": None for h in HORIZONS}
    if hist.empty:
        return out
    cur = float(hist.iloc[0]["nav_cny"])
    if cur <= 0:
        return out
    for h in HORIZONS:
        if len(hist) > h:
            base = float(hist.iloc[h]["nav_cny"])
            if base > 0:
                out[f"ret_{h}d"] = cur / base - 1.0
    return out


def _write_report_files(report: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    latest = REPORT_DIR / "latest_report.json"
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    latest.write_text(text, encoding="utf-8")
    as_of = report.get("as_of") or "unknown"
    archive = REPORT_DIR / f"report_{as_of}.json"
    archive.write_text(text, encoding="utf-8")
    return latest


def _account_export_dir(account_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(account_id))
    return ACCOUNTS_DIR / safe


def export_shadow_account_monitoring(
    conn,
    account_id: str,
    *,
    as_of: dt.date | None = None,
    horizons: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """把单户监测写到 data/shadow_farm/accounts/<id>/（CSV + JSON），便于复盘。

    账户从库中 prune 后磁盘归档仍保留（不随户删）。
    """
    meta_row = conn.execute(
        """
        SELECT account_id, strategy_id, cohort_id, template_id, initial_cash, note,
               cast(created_at AS VARCHAR), kind
        FROM paper_account WHERE account_id = ?
        """,
        [account_id],
    ).fetchone()
    nav_df = conn.execute(
        """
        SELECT as_of, cohort_id, strategy_id, cash_cny, market_value_cny, nav_cny,
               filled, pending, rejected, skipped
        FROM shadow_nav_daily
        WHERE account_id = ?
        ORDER BY as_of
        """,
        [account_id],
    ).df()
    trades_df = conn.execute(
        """
        SELECT trade_id, advice_id, symbol, side, shares, price, notional, fees,
               trade_date, source, reject_reason, created_at
        FROM paper_trades
        WHERE account_id = ?
        ORDER BY trade_date, created_at, trade_id
        """,
        [account_id],
    ).df()
    pos_df = conn.execute(
        """
        SELECT symbol, shares, avg_cost, opened_at, updated_at
        FROM paper_positions
        WHERE account_id = ?
        ORDER BY symbol
        """,
        [account_id],
    ).df()

    out_dir = _account_export_dir(account_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    nav_path = out_dir / "nav_daily.csv"
    trades_path = out_dir / "trades.csv"
    pos_path = out_dir / "positions.csv"
    meta_path = out_dir / "meta.json"
    latest_path = out_dir / "latest.json"

    nav_df.to_csv(nav_path, index=False, encoding="utf-8-sig")
    trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
    pos_df.to_csv(pos_path, index=False, encoding="utf-8-sig")

    if horizons is None and as_of is not None:
        horizons = _horizon_returns(conn, account_id, as_of)
    elif horizons is None:
        horizons = {f"ret_{h}d": None for h in HORIZONS}

    last_nav = None
    if not nav_df.empty:
        last = nav_df.iloc[-1]
        last_nav = {
            "as_of": str(last["as_of"]),
            "cash_cny": float(last["cash_cny"]),
            "market_value_cny": float(last["market_value_cny"]),
            "nav_cny": float(last["nav_cny"]),
        }

    meta = {
        "account_id": account_id,
        "strategy_id": meta_row[1] if meta_row else None,
        "cohort_id": meta_row[2] if meta_row else None,
        "template_id": meta_row[3] if meta_row else None,
        "initial_cash": float(meta_row[4]) if meta_row and meta_row[4] is not None else None,
        "note": meta_row[5] if meta_row else None,
        "created_at": meta_row[6] if meta_row else None,
        "kind": meta_row[7] if meta_row else "shadow",
        "exported_at": dt.datetime.now().astimezone().isoformat(),
        "as_of": as_of.isoformat() if as_of else (last_nav or {}).get("as_of"),
        "horizons": {
            k: (round(v, 6) if isinstance(v, (int, float)) else None)
            for k, v in (horizons or {}).items()
        },
        "last_nav": last_nav,
        "nav_rows": int(len(nav_df)),
        "trade_rows": int(len(trades_df)),
        "position_rows": int(len(pos_df)),
        "files": {
            "nav_daily_csv": str(nav_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "trades_csv": str(trades_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "positions_csv": str(pos_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "meta_json": str(meta_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "latest_json": str(latest_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        },
    }
    meta_text = json.dumps(meta, ensure_ascii=False, indent=2, default=str)
    meta_path.write_text(meta_text, encoding="utf-8")
    latest_path.write_text(meta_text, encoding="utf-8")
    return meta


def export_shadow_monitoring_bundle(
    conn,
    *,
    as_of: dt.date,
    active_accounts: list[dict[str, Any]],
    strategy_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """导出全部活跃影子户监测 + 横截面面板 CSV/JSON。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    hz_by_aid = {
        str(r.get("account_id")): (r.get("horizons") or {})
        for r in (strategy_rows or [])
        if r.get("account_id")
    }
    exported: list[dict[str, Any]] = []
    for acc in active_accounts:
        aid = str(acc.get("account_id") or "")
        if not aid:
            continue
        try:
            meta = export_shadow_account_monitoring(
                conn,
                aid,
                as_of=as_of,
                horizons=hz_by_aid.get(aid),
            )
            exported.append(
                {
                    "account_id": aid,
                    "cohort_id": acc.get("cohort_id") or meta.get("cohort_id"),
                    "strategy_id": acc.get("strategy_id") or meta.get("strategy_id"),
                    "dir": str(_account_export_dir(aid).relative_to(PROJECT_ROOT)).replace(
                        "\\", "/"
                    ),
                    "nav_rows": meta.get("nav_rows"),
                    "trade_rows": meta.get("trade_rows"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("导出影子账户 %s 失败: %s", aid, exc)

    # 横截面：当日所有有净值快照的影子户（含刚盯市的）
    panel = conn.execute(
        """
        SELECT n.account_id, n.cohort_id, n.strategy_id, n.as_of,
               n.cash_cny, n.market_value_cny, n.nav_cny,
               n.filled, n.pending, n.rejected, n.skipped,
               a.created_at
        FROM shadow_nav_daily n
        LEFT JOIN paper_account a ON a.account_id = n.account_id
        WHERE n.as_of = ?
        ORDER BY n.cohort_id DESC NULLS LAST, n.strategy_id
        """,
        [as_of],
    ).df()
    # 附上 horizons（按账户现算，面板分析用）
    if not panel.empty:
        ret_cols = {f"ret_{h}d": [] for h in HORIZONS}
        for aid in panel["account_id"].astype(str):
            hz = hz_by_aid.get(aid) or _horizon_returns(conn, aid, as_of)
            for h in HORIZONS:
                key = f"ret_{h}d"
                v = hz.get(key)
                ret_cols[key].append(round(v, 6) if isinstance(v, (int, float)) else None)
        for k, vals in ret_cols.items():
            panel[k] = vals

    panel_csv = REPORT_DIR / "panel_latest.csv"
    panel_json = REPORT_DIR / "panel_latest.json"
    panel.to_csv(panel_csv, index=False, encoding="utf-8-sig")
    panel_records = json.loads(panel.to_json(orient="records", date_format="iso", force_ascii=False))
    panel_json.write_text(
        json.dumps(panel_records, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # 按 as_of 归档一份面板，方便周复盘
    panel_arch = REPORT_DIR / f"panel_{as_of.isoformat()}.csv"
    panel.to_csv(panel_arch, index=False, encoding="utf-8-sig")

    index = {
        "as_of": as_of.isoformat(),
        "exported_at": dt.datetime.now().astimezone().isoformat(),
        "retention_days": RETENTION_DAYS,
        "max_concurrent_accounts": MAX_CONCURRENT_ACCOUNTS,
        "active_accounts": len(active_accounts),
        "exported_accounts": len(exported),
        "accounts": exported,
        "panel_csv": str(panel_csv.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "panel_json": str(panel_json.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "panel_archive_csv": str(panel_arch.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "accounts_dir": str(ACCOUNTS_DIR.relative_to(PROJECT_ROOT)).replace("\\", "/"),
    }
    index_path = REPORT_DIR / "accounts_index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return index


def load_latest_shadow_report(conn=None) -> dict[str, Any] | None:
    own = conn is None
    if conn is None:
        conn = get_connection()
        init_schema(conn)
    try:
        row = conn.execute(
            """
            SELECT report_json FROM shadow_run_log
            WHERE status IN ('ok', 'skipped_data') AND report_json IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 shadow_run_log 失败: %s", exc)
    finally:
        if own:
            conn.close()
    path = REPORT_DIR / "latest_report.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def report_to_markdown(report: dict[str, Any]) -> str:
    hz_keys = [f"ret_{h}d" for h in (report.get("horizons_days") or list(HORIZONS))]
    hz_header = " | ".join(hz_keys)
    lines = [
        "# 影子仓农场体检报告",
        "",
        f"- 运行状态：{report.get('status')}",
        f"- 净值日（行情 as_of）：{report.get('as_of')}",
        f"- 本周队列 cohort_id：{report.get('cohort_id')}",
        f"- 留存天数：{report.get('retention_days') or RETENTION_DAYS}",
        f"- 同时活跃上限（粗算）：{report.get('max_concurrent_accounts') or MAX_CONCURRENT_ACCOUNTS}",
        f"- 留存期内账户数 / 周数："
        f"{report.get('active_accounts')} / {report.get('active_cohorts')}",
        f"- 监测落盘：{((report.get('monitoring_export') or {}).get('accounts_dir')) or 'data/shadow_farm/accounts'}",
        f"- 说明：{report.get('message') or ''}",
        f"- 生成时间：{report.get('finished_at') or report.get('started_at')}",
        "",
        "> 短窗影子盈亏仅供监控体检，不构成投资建议，也不用于自动晋升冠军模型。"
        "每周新建 8 户，账户至少留存 60 天。",
        "",
        "## 本周队列各策略净值与 horizons",
        "",
        f"| 策略 | 标签 | 净值 | 现金 | 市值 | 成交 | 待开盘 | {hz_header} |",
        "|" + "|".join(["------"] * (7 + len(hz_keys))) + "|",
    ]
    for row in report.get("strategies") or []:
        hz = row.get("horizons") or {}

        def _pct(k: str) -> str:
            v = hz.get(k)
            return f"{v:.2%}" if isinstance(v, (int, float)) else "—"

        hz_cells = " | ".join(_pct(k) for k in hz_keys)
        lines.append(
            f"| {row.get('strategy_id')} | {row.get('label')} | "
            f"{row.get('nav_cny')} | {row.get('cash_cny')} | {row.get('market_value_cny')} | "
            f"{row.get('filled')} | {row.get('pending')} | {hz_cells} |"
        )
    lines.append("")
    return "\n".join(lines)



def run_shadow_farm_once(
    *,
    as_of: dt.date | None = None,
    open_new_cohort: bool | None = None,
    retention_days: int = RETENTION_DAYS,
) -> dict[str, Any]:
    """夜间/周末主入口。

    - 每周至少新建一队列 8 户（open_new_cohort=True 或本周尚无队列时自动建）。
    - 本周队列：可按最新建议模拟换票；往周未满 retention_days 的账户只盯市。
    - 超过留存期的影子账户删除（净值快照行保留）。
    """
    started = dt.datetime.now().astimezone()
    run_id = f"shadow-{started.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    calendar_day = as_of or dt.date.today()
    current_cohort = cohort_id_for_date(calendar_day)

    with write_session(init=True) as conn:
        conn.execute(
            """
            INSERT INTO shadow_run_log (run_id, started_at, status, as_of, message)
            VALUES (?, ?, 'running', ?, ?)
            """,
            [run_id, started.replace(tzinfo=None), calendar_day, "started"],
        )

    try:
        quote_asof: dt.date | None = None
        with write_session(init=True) as conn:
            _tag_legacy_shadow_cohorts(conn)
            quote_asof = last_quote_date(conn, calendar_day)
            if quote_asof is None:
                report = {
                    "run_id": run_id,
                    "status": "skipped_data",
                    "as_of": None,
                    "cohort_id": current_cohort,
                    "started_at": started.isoformat(),
                    "finished_at": dt.datetime.now().astimezone().isoformat(),
                    "message": "仓库无可用日行情，跳过影子仓（依赖日更先完成）",
                    "strategies": [],
                    "disclaimer": "短窗影子盈亏不进入 retrain/promotion",
                }
                _finalize_run(run_id, report)
                return report

            pool_cards: dict[str, tuple[str | None, list[dict]]] = {}
            for pool in ("hs", "bj"):
                pool_cards[pool] = load_latest_advice_cards(conn, pool_id=pool)

            existing_week = conn.execute(
                """
                SELECT count(DISTINCT strategy_id) FROM paper_account
                WHERE coalesce(kind, 'human') = 'shadow' AND cohort_id = ?
                """,
                [current_cohort],
            ).fetchone()[0]
            need_create = int(existing_week) < len(STRATEGY_SPECS)
            # True=周末/手动：本周队列换票；False/None=日更：只盯市（本周缺户时仍会建户并补一笔）
            force_rebalance = open_new_cohort is True
            rebalance_current = force_rebalance or need_create

        accounts = ensure_shadow_cohort(current_cohort)
        if need_create or force_rebalance:
            logger.info(
                "影子仓周队列 %s 已就绪（%d 户，rebalance=%s）",
                current_cohort,
                len(accounts),
                rebalance_current,
            )

        # 换票必须在 write_session 外调用 simulate_advice_cards（其内部自开写锁，嵌套会死锁）
        sim_by_strategy: dict[str, dict[str, Any]] = {}
        notes: list[str] = []
        if rebalance_current:
            for spec in STRATEGY_SPECS:
                aid = accounts[spec.strategy_id]
                as_of_s, cards = pool_cards[spec.pool_id]
                filled = pending = rejected = skipped = 0
                sim_note = ""
                if not cards or not as_of_s:
                    sim_note = f"无 {spec.pool_id} 建议快照，仅盯市"
                    notes.append(f"{spec.strategy_id}: 无建议快照")
                else:
                    with write_session(init=True) as conn:
                        signal_date = parse_as_of_date(as_of_s)
                        sim_cards = cards_for_strategy(
                            conn,
                            cards,
                            spec,
                            signal_date=signal_date,
                            account_id=aid,
                            quote_asof=quote_asof,
                        )
                    if sim_cards:
                        sm = simulate_advice_cards(aid, sim_cards, as_of_nav=quote_asof)
                        filled, pending, rejected, skipped = (
                            sm.filled,
                            sm.pending,
                            sm.rejected,
                            sm.skipped,
                        )
                        sim_note = (
                            f"cohort={current_cohort} advice_as_of={as_of_s} "
                            f"cards={len(sim_cards)} filled={filled} pending={pending}"
                        )
                    else:
                        sim_note = (
                            f"cohort={current_cohort} advice_as_of={as_of_s} 无可模拟卡片"
                        )
                sim_by_strategy[spec.strategy_id] = {
                    "filled": filled,
                    "pending": pending,
                    "rejected": rejected,
                    "skipped": skipped,
                    "note": sim_note,
                }

        strategy_rows: list[dict[str, Any]] = []
        mtm_extra = 0
        pruned = 0
        active_after: list[dict[str, Any]] = []
        cohort_n = 0

        with write_session(init=True) as conn:
            active = list_active_shadow_accounts(
                conn, as_of=calendar_day, retention_days=retention_days
            )
            for spec in STRATEGY_SPECS:
                aid = accounts[spec.strategy_id]
                sim = sim_by_strategy.get(spec.strategy_id) or {}
                if not rebalance_current:
                    filled = pending = rejected = skipped = 0
                    sim_note = f"cohort={current_cohort} 日更仅盯市（换票靠周末链路）"
                else:
                    filled = int(sim.get("filled") or 0)
                    pending = int(sim.get("pending") or 0)
                    rejected = int(sim.get("rejected") or 0)
                    skipped = int(sim.get("skipped") or 0)
                    sim_note = str(sim.get("note") or "")

                nav = mark_to_market_nav(conn, aid, quote_asof)
                _upsert_nav(
                    conn,
                    aid,
                    spec.strategy_id,
                    quote_asof,
                    nav,
                    cohort_id=current_cohort,
                    filled=filled,
                    pending=pending,
                    rejected=rejected,
                    skipped=skipped,
                )
                horizons = _horizon_returns(conn, aid, quote_asof)
                strategy_rows.append(
                    {
                        "strategy_id": spec.strategy_id,
                        "label": spec.label,
                        "pool_id": spec.pool_id,
                        "cohort_id": current_cohort,
                        "account_id": aid,
                        "cash_cny": round(nav["cash_cny"], 2),
                        "market_value_cny": round(nav["market_value_cny"], 2),
                        "nav_cny": round(nav["nav_cny"], 2),
                        "filled": filled,
                        "pending": pending,
                        "rejected": rejected,
                        "skipped": skipped,
                        "horizons": {
                            k: (round(v, 6) if v is not None else None)
                            for k, v in horizons.items()
                        },
                        "note": sim_note,
                    }
                )

            current_aids = set(accounts.values())
            for acc in active:
                aid = acc["account_id"]
                if aid in current_aids:
                    continue
                sid = acc.get("strategy_id") or "unknown"
                cid = acc.get("cohort_id") or "legacy"
                nav = mark_to_market_nav(conn, aid, quote_asof)
                _upsert_nav(
                    conn,
                    aid,
                    sid,
                    quote_asof,
                    nav,
                    cohort_id=cid,
                    filled=0,
                    pending=0,
                    rejected=0,
                    skipped=0,
                )
                mtm_extra += 1

            pruned = prune_expired_shadow_accounts(
                conn, as_of=calendar_day, retention_days=retention_days
            )
            active_after = list_active_shadow_accounts(
                conn, as_of=calendar_day, retention_days=retention_days
            )
            cohort_n = len({a["cohort_id"] for a in active_after})

        monitoring_export: dict[str, Any] | None = None
        try:
            with write_session(init=True) as conn:
                monitoring_export = export_shadow_monitoring_bundle(
                    conn,
                    as_of=quote_asof,
                    active_accounts=active_after,
                    strategy_rows=strategy_rows,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("影子仓监测落盘失败: %s", exc)

        status = "ok"
        if rebalance_current:
            msg = (
                f"影子仓跑批完成：本周队列 {current_cohort}（{len(STRATEGY_SPECS)} 户换票+盯市）；"
                f"另盯市往周 {mtm_extra} 户；留存期内共 {len(active_after)} 户 / {cohort_n} 周"
                f"（上限约 {MAX_CONCURRENT_ACCOUNTS}）"
            )
        else:
            msg = (
                f"影子仓日更盯市：本周队列 {current_cohort}；"
                f"另盯市往周 {mtm_extra} 户；留存期内共 {len(active_after)} 户 / {cohort_n} 周"
                f"（上限约 {MAX_CONCURRENT_ACCOUNTS}）"
            )
        if pruned:
            msg += f"；清理过期 {pruned} 户"
        if monitoring_export:
            msg += f"；已落盘 {monitoring_export.get('exported_accounts')} 户监测文件"
        if all((pool_cards[p][1] == [] for p in ("hs", "bj"))):
            status = "skipped_data"
            msg = "两侧建议快照皆空：已盯市记账，未新开仓（请白天生成建议或等周末链路）"
        elif notes:
            msg = msg + "；" + "；".join(notes[:4])

        finished = dt.datetime.now().astimezone()
        report = {
            "run_id": run_id,
            "status": status,
            "as_of": quote_asof.isoformat(),
            "calendar_day": calendar_day.isoformat(),
            "cohort_id": current_cohort,
            "retention_days": retention_days,
            "max_concurrent_accounts": MAX_CONCURRENT_ACCOUNTS,
            "active_accounts": len(active_after),
            "active_cohorts": cohort_n,
            "mtm_prior_cohort_accounts": mtm_extra,
            "pruned_accounts": pruned,
            "opened_or_ensured_cohort": need_create or force_rebalance,
            "rebalanced_current_cohort": rebalance_current,
            "monitoring_export": monitoring_export,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "message": msg,
            "strategies": strategy_rows,
            "horizons_days": list(HORIZONS),
            "disclaimer": "短窗影子盈亏仅供体检监控，不进入 retrain/promotion，不构成投资建议",
        }
        _finalize_run(run_id, report)
        return report
    except Exception as exc:  # noqa: BLE001
        logger.exception("shadow farm 失败")
        report = {
            "run_id": run_id,
            "status": "failed",
            "as_of": None,
            "cohort_id": current_cohort,
            "started_at": started.isoformat(),
            "finished_at": dt.datetime.now().astimezone().isoformat(),
            "message": f"{type(exc).__name__}: {exc}",
            "strategies": [],
            "disclaimer": "短窗影子盈亏不进入 retrain/promotion",
        }
        _finalize_run(run_id, report)
        raise


def _finalize_run(run_id: str, report: dict[str, Any]) -> None:
    try:
        _write_report_files(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 shadow_farm JSON 失败: %s", exc)
    as_of = None
    if report.get("as_of"):
        try:
            as_of = dt.date.fromisoformat(str(report["as_of"])[:10])
        except ValueError:
            as_of = None
    try:
        with write_session(init=True) as conn:
            conn.execute(
                """
                UPDATE shadow_run_log
                SET finished_at = ?, status = ?, as_of = ?, message = ?, report_json = ?
                WHERE run_id = ?
                """,
                [
                    dt.datetime.now().replace(tzinfo=None),
                    report.get("status") or "failed",
                    as_of,
                    (report.get("message") or "")[:2000],
                    json.dumps(report, ensure_ascii=False, default=str),
                    run_id,
                ],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写 shadow_run_log 失败（报告文件可能已落盘）: %s", exc)


def shadow_nav_frame(conn) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT account_id, cohort_id, strategy_id, as_of, cash_cny, market_value_cny, nav_cny,
               filled, pending, rejected, skipped
        FROM shadow_nav_daily
        ORDER BY as_of DESC, cohort_id DESC NULLS LAST, strategy_id
        """
    ).df()
