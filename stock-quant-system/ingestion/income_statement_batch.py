"""
Phase 0.6 数据缺口补齐（第二轮）—— 重量批次二：利润表绝对值批量抓取。

主数据源：Tushare Pro `income`（2000 积分档可用；`income_vip` 整表拉无权限）。
一次调用返回该股票全部历史合并报表，约 1 秒/只，且带 ann_date / f_ann_date。

原先用新浪 `ak.stock_financial_report_sina`：夜间频繁 JSONDecodeError，单只重试到 30–40 秒，
全市场剩余部分要再跑约 8 小时。新浪路径仍保留，仅在显式 `--source sina` 时使用。

落地为 EAV（与 fundamentals 一致）。Tushare 英文字段映射为与新浪一致的中文科目名，
便于 Phase 1 用 `营业总收入` 等做 PS / 体量对比。PIT 对齐仍 JOIN disclosure_calendar；
Tushare 的公告日目前未写入本表，避免改主键。
"""
from __future__ import annotations

import logging

import akshare as ak
import pandas as pd
from tqdm import tqdm

from common.config import get_config
from common.db import get_connection, init_schema
from common.http_retry import FetchFailedError, polite_sleep, retry_on_failure
from common.tushare_client import get_pro_api, to_ts_code

logger = logging.getLogger("ingestion.income_statement_batch")

# Tushare 字段 -> 与新浪利润表对齐的中文科目（Phase 1 按中文名取值）
_TS_INDICATOR_MAP = {
    "basic_eps": "基本每股收益",
    "diluted_eps": "稀释每股收益",
    "total_revenue": "营业总收入",
    "revenue": "营业收入",
    "int_income": "利息收入",
    "prem_earned": "已赚保费",
    "comm_income": "手续费及佣金收入",
    "n_commis_income": "手续费及佣金净收入",
    "n_oth_income": "其他经营净收益",
    "n_oth_b_income": "加:其他业务净收益",
    "prem_income": "保险业务收入",
    "out_prem": "减:分出保费",
    "une_prem_reser": "提取未到期责任准备金",
    "reins_income": "其中:分保费收入",
    "n_sec_tb_income": "代理买卖证券业务净收入",
    "n_sec_uw_income": "证券承销业务净收入",
    "n_asset_mg_income": "受托客户资产管理业务净收入",
    "oth_b_income": "其他业务收入",
    "fv_value_chg_gain": "加:公允价值变动净收益",
    "invest_income": "加:投资净收益",
    "ass_invest_income": "其中:对联营企业和合营企业的投资收益",
    "forex_gain": "加:汇兑净收益",
    "total_cogs": "营业总成本",
    "oper_cost": "减:营业成本",
    "int_exp": "减:利息支出",
    "comm_exp": "减:手续费及佣金支出",
    "biz_tax_surchg": "减:营业税金及附加",
    "sell_exp": "减:销售费用",
    "admin_exp": "减:管理费用",
    "fin_exp": "减:财务费用",
    "assets_impair_loss": "减:资产减值损失",
    "prem_refund": "退保金",
    "compens_payout": "赔付总支出",
    "reser_insur_liab": "提取保险责任准备金",
    "div_payt": "保户红利支出",
    "reins_exp": "分保费用",
    "oper_exp": "营业支出",
    "compens_payout_refu": "减:摊回赔付支出",
    "insur_reser_refu": "减:摊回保险责任准备金",
    "reins_cost_refund": "减:摊回分保费用",
    "other_bus_cost": "其他业务成本",
    "operate_profit": "营业利润",
    "non_oper_income": "加:营业外收入",
    "non_oper_exp": "减:营业外支出",
    "nca_disploss": "其中:减:非流动资产处置净损失",
    "total_profit": "利润总额",
    "income_tax": "减:所得税费用",
    "n_income": "净利润(含少数股东损益)",
    "n_income_attr_p": "净利润(不含少数股东损益)",
    "minority_gain": "少数股东损益",
    "oth_compr_income": "其他综合收益",
    "t_compr_income": "综合收益总额",
    "compr_inc_attr_p": "归属于母公司(或股东)的综合收益总额",
    "compr_inc_attr_m_s": "归属于少数股东的综合收益总额",
    "ebit": "息税前利润",
    "ebitda": "息税折旧摊销前利润",
    "insurance_exp": "保险业务支出",
    "undist_profit": "年初未分配利润",
    "distable_profit": "可分配利润",
    "rd_exp": "研发费用",
    "fin_exp_int_exp": "财务费用:利息费用",
    "fin_exp_int_inc": "财务费用:利息收入",
    "continued_net_profit": "持续经营净利润",
    "end_net_profit": "终止经营净利润",
    "credit_impa_loss": "信用减值损失",
    "asset_disp_income": "资产处置收益",
    "oth_income": "其他收益",
    "total_opcost": "营业总成本2",
    "amodcost_fin_assets": "以摊余成本计量的金融资产终止确认收益",
    "oth_impair_loss_assets": "其他资产减值损失",
    "net_after_nr_lp_correct": "扣除非经常性损益后的净利润",
}

_SKIP_COLS = {
    "ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type",
    "end_type", "update_flag",
}


@retry_on_failure()
def _fetch_income_tushare(ts_code: str) -> pd.DataFrame:
    return get_pro_api().income(ts_code=ts_code, report_type="1")


@retry_on_failure()
def _fetch_income_sina(stock_with_exchange: str) -> pd.DataFrame:
    return ak.stock_financial_report_sina(stock=stock_with_exchange, symbol="利润表")


_META_COLS = {"报告日", "数据源", "是否审计", "公告日期", "币种", "类型", "更新日期"}


def _tushare_to_long(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty or "end_date" not in df.columns:
        return pd.DataFrame(columns=["symbol", "report_date", "indicator", "value"])

    out = df.copy()
    if "report_type" in out.columns:
        out = out[out["report_type"].astype(str) == "1"]
    out["report_date"] = pd.to_datetime(out["end_date"], format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["report_date"])
    out = out.drop_duplicates(subset=["end_date"], keep="last")

    value_cols = [c for c in out.columns if c not in _SKIP_COLS and c != "report_date"]
    long_df = out.melt(id_vars=["report_date"], value_vars=value_cols, var_name="indicator", value_name="value")
    long_df["indicator"] = long_df["indicator"].map(lambda x: _TS_INDICATOR_MAP.get(x, x))
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    long_df = long_df.dropna(subset=["value"])
    long_df["report_date"] = long_df["report_date"].dt.date
    long_df["symbol"] = symbol
    return long_df[["symbol", "report_date", "indicator", "value"]].drop_duplicates(
        subset=["symbol", "report_date", "indicator"]
    )


def _sina_to_long(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty or "报告日" not in df.columns:
        return pd.DataFrame(columns=["symbol", "report_date", "indicator", "value"])

    df = df.copy()
    df["report_date"] = pd.to_datetime(df["报告日"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["report_date"])
    value_cols = [c for c in df.columns if c not in _META_COLS and c != "report_date"]

    long_df = df.melt(id_vars=["report_date"], value_vars=value_cols, var_name="indicator", value_name="value")
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    long_df = long_df.dropna(subset=["value"])
    long_df["report_date"] = long_df["report_date"].dt.date
    long_df["symbol"] = symbol
    return long_df[["symbol", "report_date", "indicator", "value"]].drop_duplicates(
        subset=["symbol", "report_date", "indicator"]
    )


def _get_sync_targets(conn, symbols: list[str] | None, source: str) -> list[tuple[str, str]]:
    """返回 (symbol, fetch_key)。Tushare 为 ts_code，新浪为 sh600519。"""
    exchanges = ("sh", "sz", "bj") if source == "tushare" else ("sh", "sz")
    placeholders_ex = ",".join(["?"] * len(exchanges))
    where = f"WHERE is_delisted = FALSE AND exchange IN ({placeholders_ex})"
    params: list = list(exchanges)
    if symbols:
        placeholders = ",".join(["?"] * len(symbols))
        where += f" AND symbol IN ({placeholders})"
        params.extend(symbols)
    rows = conn.execute(f"SELECT symbol, exchange FROM universe {where}", params).fetchall()
    if source == "tushare":
        return [(sym, to_ts_code(sym, exch)) for sym, exch in rows]
    return [(sym, f"{exch}{sym}") for sym, exch in rows]


def _upsert_income_statement(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    conn.register("incoming_income", df)
    conn.execute(
        """
        DELETE FROM income_statement
        WHERE (symbol, report_date, indicator) IN (
            SELECT symbol, report_date, indicator FROM incoming_income
        )
        """
    )
    conn.execute(
        """
        INSERT INTO income_statement (symbol, report_date, indicator, value)
        SELECT symbol, report_date, indicator, value FROM incoming_income
        """
    )
    conn.unregister("incoming_income")
    return len(df)


def sync_income_statement_batch(
    symbols: list[str] | None = None,
    limit: int | None = None,
    skip_existing: bool = False,
    source: str = "tushare",
) -> dict:
    checkpoint_size = get_config()["ingestion"].get("checkpoint_batch_size", 50)

    conn = get_connection()
    init_schema(conn)
    targets = _get_sync_targets(conn, symbols, source)
    if skip_existing:
        done = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM income_statement").fetchall()}
        before = len(targets)
        targets = [(sym, key) for sym, key in targets if sym not in done]
        logger.info("skip_existing=True：跳过已有利润表的股票 %d 只，剩余待抓 %d 只", before - len(targets), len(targets))
    if limit:
        targets = targets[:limit]

    stats = {"ok": 0, "empty": 0, "failed": 0, "rows_written": 0, "failed_symbols": [], "source": source}
    to_long = _tushare_to_long if source == "tushare" else _sina_to_long
    fetch_fn = _fetch_income_tushare if source == "tushare" else _fetch_income_sina

    for i, (symbol, fetch_key) in enumerate(tqdm(targets, desc=f"income_statement_batch[{source}]")):
        try:
            raw = fetch_fn(fetch_key)
            long_df = to_long(raw, symbol)
            if long_df.empty:
                stats["empty"] += 1
            else:
                n = _upsert_income_statement(conn, long_df)
                stats["rows_written"] += n
                stats["ok"] += 1
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_income_statement", symbol, "success", f"source={source} rows={len(long_df)}"],
            )
        except (FetchFailedError, Exception) as exc:  # noqa: BLE001
            stats["failed"] += 1
            stats["failed_symbols"].append(symbol)
            conn.execute(
                "INSERT INTO sync_log (task_name, symbol, status, message) VALUES (?, ?, ?, ?)",
                ["sync_income_statement", symbol, "failed", f"{type(exc).__name__}: {str(exc)[:280]}"],
            )
            logger.warning("%s 利润表抓取失败，已记录，跳过: %s", symbol, exc)

        if (i + 1) % checkpoint_size == 0:
            logger.info(
                "进度 %d/%d: ok=%d empty=%d failed=%d rows=%d",
                i + 1, len(targets), stats["ok"], stats["empty"], stats["failed"], stats["rows_written"],
            )

        polite_sleep()

    conn.close()
    logger.info("利润表绝对值批量同步完成: %s", stats)
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="全市场利润表绝对值批量抓取")
    parser.add_argument("--limit", type=int, default=None, help="仅同步前N只股票（调试用）")
    parser.add_argument("--symbols", type=str, default=None, help="逗号分隔的股票代码列表")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="已有利润表记录的股票直接跳过（中断后续传）",
    )
    parser.add_argument(
        "--source",
        choices=["tushare", "sina"],
        default="tushare",
        help="默认 tushare；sina 仅作备用（夜间容易空响应重试）",
    )
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None
    result = sync_income_statement_batch(
        symbols=symbols, limit=args.limit, skip_existing=args.skip_existing, source=args.source
    )
    print(result)
