"""
股票池（universe）同步：获取全市场股票代码与名称，推断交易所/板块，标记 ST 状态。

数据源选择说明：
    ak.stock_info_a_code_name() 基于上交所/深交所官方公开列表，实测在本机网络环境下
    比东方财富(eastmoney)接口更稳定，作为股票池主数据源。它只提供 code/name，
    交易所与板块通过代码前缀规则本地推断（见 classify_symbol）。

    北交所(bj)历史上部分代码段与"曾经的新三板精选层"重叠，前缀规则会随时间演化，
    如后续发现分类偏差，应在此处集中修正，不影响下游任何模块。
"""
from __future__ import annotations

import datetime as dt
import logging

import akshare as ak
import pandas as pd

from common.db import get_connection, init_schema
from common.http_retry import retry_on_failure

logger = logging.getLogger("ingestion.universe")

# 代码前缀 -> (交易所, 板块)。按最长前缀优先匹配。
_PREFIX_RULES: list[tuple[str, str, str]] = [
    ("689", "sh", "star"),   # 科创板 CDR（存托凭证，如九号公司-WD）
    ("688", "sh", "star"),   # 科创板
    ("60", "sh", "main"),    # 沪主板 (600/601/603/605)
    ("300", "sz", "gem"),    # 创业板
    ("301", "sz", "gem"),
    ("302", "sz", "gem"),    # 创业板注册制扩容新增代码段
    ("000", "sz", "main"),   # 深主板
    ("001", "sz", "main"),
    ("002", "sz", "main"),   # 原中小板，2021年已并入主板
    ("003", "sz", "main"),
    ("92", "bj", "bse"),     # 北交所新股代码段
    ("83", "bj", "bse"),
    ("87", "bj", "bse"),
    ("88", "bj", "bse"),
    ("43", "bj", "bse"),
]


def classify_symbol(code: str) -> tuple[str, str]:
    for prefix, exchange, board in sorted(_PREFIX_RULES, key=lambda r: -len(r[0])):
        if code.startswith(prefix):
            return exchange, board
    return "unknown", "unknown"


def is_st(name: str) -> bool:
    return "ST" in name.upper()


@retry_on_failure()
def _fetch_code_name() -> pd.DataFrame:
    return ak.stock_info_a_code_name()


def sync_universe() -> pd.DataFrame:
    """拉取全市场股票代码/名称，写入 universe 表（增量 upsert），返回本次同步的完整快照。"""
    logger.info("开始同步全市场股票池...")
    raw = _fetch_code_name()
    raw = raw.rename(columns={"code": "symbol", "name": "name"})
    raw["symbol"] = raw["symbol"].astype(str).str.zfill(6)

    exch_board = raw["symbol"].map(classify_symbol)
    raw["exchange"] = exch_board.map(lambda t: t[0])
    raw["board"] = exch_board.map(lambda t: t[1])
    raw["is_st"] = raw["name"].map(is_st)

    today = dt.date.today()
    raw["last_seen_date"] = today

    conn = get_connection()
    init_schema(conn)
    try:
        conn.register("incoming_universe", raw[["symbol", "exchange", "board", "name", "is_st", "last_seen_date"]])

        # 已存在的股票：更新 name/is_st/last_seen_date，first_seen_date 保持不变
        conn.execute(
            """
            UPDATE universe AS u
            SET name = i.name,
                is_st = i.is_st,
                exchange = i.exchange,
                board = i.board,
                last_seen_date = i.last_seen_date,
                updated_at = current_timestamp
            FROM incoming_universe AS i
            WHERE u.symbol = i.symbol
            """
        )

        # 新股票：插入，first_seen_date = 本次同步日期
        conn.execute(
            """
            INSERT INTO universe (symbol, exchange, board, name, is_st, is_delisted, first_seen_date, last_seen_date)
            SELECT i.symbol, i.exchange, i.board, i.name, i.is_st, FALSE, i.last_seen_date, i.last_seen_date
            FROM incoming_universe AS i
            WHERE NOT EXISTS (SELECT 1 FROM universe u WHERE u.symbol = i.symbol)
            """
        )

        # 本次快照中不存在、但历史上出现过的股票：标记为疑似退市/停牌超期（需人工/后续流程复核）
        conn.execute(
            """
            UPDATE universe
            SET is_delisted = TRUE, updated_at = current_timestamp
            WHERE symbol NOT IN (SELECT symbol FROM incoming_universe)
              AND is_delisted = FALSE
            """
        )

        n_total = conn.execute("SELECT count(*) FROM universe").fetchone()[0]
        n_st = conn.execute("SELECT count(*) FROM universe WHERE is_st").fetchone()[0]
        n_delisted = conn.execute("SELECT count(*) FROM universe WHERE is_delisted").fetchone()[0]
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_universe", "success", f"total={n_total}, st={n_st}, delisted_flagged={n_delisted}"],
        )
        logger.info("股票池同步完成: 全市场=%d, ST=%d, 标记退市/停牌超期=%d", n_total, n_st, n_delisted)
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "INSERT INTO sync_log (task_name, status, message) VALUES (?, ?, ?)",
            ["sync_universe", "failed", str(exc)[:500]],
        )
        raise
    finally:
        conn.close()

    return raw


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    df = sync_universe()
    print(f"本次同步股票数: {len(df)}")
