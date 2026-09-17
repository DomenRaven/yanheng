"""审计 limit_price 与 daily_quotes 对齐率（entry_rules 拦截可信度）。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.db import get_connection, init_schema


def main() -> None:
    conn = get_connection()
    init_schema(conn)
    try:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_quotes WHERE adjust='qfq'").fetchone()
        if not row or row[0] is None:
            print("无 daily_quotes")
            return
        d = row[0]
        q = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM daily_quotes WHERE trade_date = ? AND adjust='qfq'",
            [d],
        ).fetchone()[0]
        both = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT symbol FROM daily_quotes WHERE trade_date = ? AND adjust = 'qfq'
                INTERSECT
                SELECT symbol FROM limit_price WHERE trade_date = ?
            )
            """,
            [d, d],
        ).fetchone()[0]
        ratio = both / q if q else 0
        print(f"交易日 {d}")
        print(f"daily_quotes 标的数: {q}")
        print(f"同时有 limit_price 的标的数: {both}")
        print(f"交集覆盖率: {ratio:.1%}")
        if ratio < 0.9:
            print("警告: 覆盖率偏低，entry_blocked_at_open 回测拦截率可能失真")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
