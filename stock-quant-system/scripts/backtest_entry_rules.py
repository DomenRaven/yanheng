"""Phase 5 阶段 C 验收：信号日收盘买 vs 次日开盘买 的成本差（简化统计）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advice.entry_rules import entry_blocked_at_open
from common.db import get_connection, init_schema


def main() -> None:
    conn = get_connection()
    init_schema(conn)
    try:
        latest = conn.execute("SELECT MAX(trade_date) FROM daily_quotes WHERE adjust='qfq'").fetchone()
        if not latest or latest[0] is None:
            print("无行情，跳过")
            return
        end = pd.Timestamp(latest[0]).date()
        start = end - pd.Timedelta(days=120)
        syms = conn.execute(
            """
            SELECT symbol FROM daily_quotes
            WHERE trade_date = ? AND adjust = 'qfq' AND close BETWEEN 5 AND 30
            LIMIT 80
            """,
            [end],
        ).df()["symbol"].tolist()
        rows = []
        for sym in syms:
            hist = conn.execute(
                """
                SELECT trade_date, close, open
                FROM daily_quotes
                WHERE symbol = ? AND adjust = 'qfq' AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                [sym, start, end],
            ).df()
            if len(hist) < 10:
                continue
            hist["trade_date"] = pd.to_datetime(hist["trade_date"]).dt.date
            for i in range(len(hist) - 1):
                sig = hist.iloc[i]["trade_date"]
                exec_d = hist.iloc[i + 1]["trade_date"]
                close_sig = float(hist.iloc[i]["close"])
                open_exec = float(hist.iloc[i + 1]["open"])
                blocked, _ = entry_blocked_at_open(conn, sym, exec_d)
                rows.append(
                    {
                        "symbol": sym,
                        "blocked": blocked,
                        "close_sig": close_sig,
                        "open_exec": open_exec,
                        "cost_diff_pct": open_exec / close_sig - 1,
                    }
                )
        df = pd.DataFrame(rows)
        if df.empty:
            print("样本不足")
            return
        n = len(df)
        block_rate = df["blocked"].mean()
        diff = df.loc[~df["blocked"], "cost_diff_pct"]
        print("=== entry_rules 简化回测（非策略收益承诺）===")
        print(f"样本笔数: {n}")
        print(f"次日开盘不可买比例: {block_rate:.1%}")
        if not diff.empty:
            print(f"可成交时 开盘/信号收盘 - 1: 均值 {diff.mean():.2%}  中位 {diff.median():.2%}")
        print("局限: 未含滑点/涨跌停全样本；择时不承诺超额。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
