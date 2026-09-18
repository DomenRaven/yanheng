"""
全市场每日打分排序输出（"今日候选清单"）。

流程：加载 mlops/registry 的**冠军模型**（只读 `champion.json` 指向的 run，见
`mlops/retrain_schedule.py` 与规格 M1）。**禁止**回退到「最新训练文件夹」。
缺少 champion.json 或 run 目录不存在时直接失败，以免静默用挑战者/未晋升模型打生产分。
-> 用 research/panel.py 的 build_asof_snapshot() 取"最近一个已同步的全市场交易日"的
point-in-time因子快照 -> 用模型打分排序 -> 剔除当天停牌/涨停封死（买不进）的股票 ->
对Top-N候选叠加 behavior/ 行为金融代理指标做冲突提示 -> 输出候选清单，同时把预测分数
写入 `prediction_log` 表（供 mlops/drift_monitor.py 做样本外漂移监控，也是Phase4
"建议可追溯到具体模型版本"验收标准的数据基础）。

注意（如实标注局限性，不夸大"能力"）：
  - 这是"排序打分"，不是"买入建议"或"收益预测"，模型训练目标是相对排序（LambdaRank），
    分数本身没有可解释的量级含义，只有排序意义。
  - behavior/ 的行为金融信号是"交叉验证/冲突提示"，不是另一个预测模型，模型排名不会
    因为behavior信号而改变，只是在展示层附加提示（对齐计划文档Phase2设计原意）。
  - 输出仅供研究参考，不构成投资建议；具体持仓需结合仓位管理、行业集中度等本系统
    Phase 4 才覆盖的风控环节。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("advice.scanner")


def _load_champion_model(
    registry_dir: str = "mlops/registry",
    *,
    pool_id: str = "hs",
) -> tuple[object, dict]:
    from advice.champion_registry import load_champion_model

    model, metadata = load_champion_model(registry_dir, pool_id=pool_id)
    logger.info(
        "加载池 %s 冠军 run_id=%s（训练于 %s，指针 %s）",
        pool_id,
        metadata.get("run_id"),
        metadata.get("trained_at"),
        metadata.get("champion_file"),
    )
    return model, metadata


# 向后兼容旧函数名
_load_latest_model = _load_champion_model


def _load_tradability_today(conn, symbols: list[str], trade_date) -> pd.DataFrame:
    placeholders = ",".join(["?"] * len(symbols))
    df = conn.execute(
        f"""
        SELECT lp.symbol,
               dq.close, lp.up_limit,
               (sc.trade_date IS NOT NULL) AS is_suspended
        FROM (SELECT unnest(?::VARCHAR[]) AS symbol) syms
        LEFT JOIN limit_price lp ON lp.symbol = syms.symbol AND lp.trade_date = ?
        LEFT JOIN daily_quotes dq ON dq.symbol = syms.symbol AND dq.trade_date = ? AND dq.adjust = 'qfq'
        LEFT JOIN suspend_calendar sc ON sc.symbol = syms.symbol AND sc.trade_date = ? AND sc.suspend_type = 'S'
        """,
        [symbols, trade_date, trade_date, trade_date],
    ).df()
    df["is_limit_up_locked"] = (df["up_limit"].notna()) & (df["close"] >= df["up_limit"] * 0.998)
    df["is_tradable"] = ~(df["is_suspended"].fillna(False) | df["is_limit_up_locked"].fillna(False))
    return df[["symbol", "is_tradable"]]


def _attach_behavior_signals(conn, symbols: list[str], as_of_date: str) -> pd.DataFrame:
    """给Top候选叠加行为金融代理指标（behavior/），产出人类可读的冲突提示列。
    只对最终展示的候选算，不对全市场算——这是"交叉验证"，不是选股因子本身。"""
    from behavior.chip_distribution import compute_disposition_proxy
    from behavior.crowding import compute_crowding_signals
    from behavior.sentiment_nlp import compute_big_small_divergence

    disposition = compute_disposition_proxy(conn, symbols, as_of_date)
    crowding = compute_crowding_signals(conn, symbols, as_of_date)
    divergence = compute_big_small_divergence(conn, symbols, as_of_date)

    out = pd.DataFrame({"symbol": symbols})
    out = (
        out.merge(disposition[["symbol", "unrealized_gain_pct", "disposition_flag"]], on="symbol", how="left")
        .merge(crowding[["symbol", "dt_count", "crowding_flag"]], on="symbol", how="left")
        .merge(divergence[["symbol", "divergence_flag"]], on="symbol", how="left")
    )

    def _conflict(row):
        """模型打分很高，但行为金融信号出现风险提示时，拼一句"冲突提示"。"""
        warnings = []
        if pd.notna(row.get("unrealized_gain_pct")) and row["unrealized_gain_pct"] > 0.30:
            warnings.append("浮盈筹码集中")
        if pd.notna(row.get("dt_count")) and row["dt_count"] >= 3:
            warnings.append("游资高频出没")
        div = row.get("divergence_flag") or ""
        if "大单卖出小单买入" in div:
            warnings.append("大单卖出小单买入(散户接盘风险)")
        return "；".join(warnings) if warnings else "无冲突"

    out["conflict_flag"] = out.apply(_conflict, axis=1)
    return out


def run_scan(
    top_n: int = 50,
    registry_dir: str = "mlops/registry",
    *,
    pool_id: str = "hs",
    source: str = "ui",
) -> tuple[pd.DataFrame, pd.DataFrame, object]:
    from advice.champion_registry import filter_snapshot_by_pool
    from advice.scan_archive import record_scan_archive
    from common.db import get_connection, init_schema
    from research.panel import build_asof_snapshot

    model, metadata = _load_champion_model(registry_dir, pool_id=pool_id)
    feature_cols = metadata["feature_cols"]

    snapshot = build_asof_snapshot()
    snapshot = filter_snapshot_by_pool(snapshot, pool_id)
    if snapshot.empty:
        raise ValueError(f"池 {pool_id} 截面为空，无法扫描")
    trade_date = snapshot["trade_date"].iloc[0]
    logger.info("快照日期: %s，池 %s 候选股票数: %d", trade_date, pool_id, len(snapshot))

    X = snapshot[feature_cols].copy()
    for c in feature_cols:
        X[c] = X[c].clip(X[c].quantile(0.01), X[c].quantile(0.99))
    snapshot = snapshot.copy()
    snapshot["pred_score"] = model.predict(X)

    conn = get_connection()
    init_schema(conn)
    try:
        tradability = _load_tradability_today(conn, snapshot["symbol"].tolist(), str(trade_date))
        names = conn.execute("SELECT symbol, name FROM universe").df()

        result = snapshot.merge(tradability, on="symbol", how="left").merge(names, on="symbol", how="left")
        result["is_tradable"] = result["is_tradable"].fillna(True)
        result["rank"] = result["pred_score"].rank(ascending=False, method="first").astype(int)
        result = result.sort_values("rank")

        cols = [
            "rank", "symbol", "name", "exchange", "is_st", "is_tradable", "pred_score", "close",
            "factor_bp", "factor_ep", "factor_roe", "factor_mom_12_1", "factor_gross_margin",
        ]
        out = result[cols]

        record_scan_archive(
            conn,
            ranked=out,
            trade_date=trade_date,
            model_run_id=metadata["run_id"],
            pool_id=pool_id,
            source=source,
        )

        top_pick = out[out["is_tradable"]].head(top_n).copy()
        behavior = _attach_behavior_signals(conn, top_pick["symbol"].tolist(), str(trade_date))
        top_pick = top_pick.merge(behavior, on="symbol", how="left")
        from advice.reason_pack import attach_reason_packs

        top_pick = attach_reason_packs(top_pick)
        from advice.industry_cap import attach_industry

        top_pick = attach_industry(conn, top_pick, trade_date)
    finally:
        conn.close()

    logger.info(
        "池 %s Top-%d 候选（剔除不可买入 %d 只）:\n%s",
        pool_id,
        top_n,
        int((~out["is_tradable"]).sum()),
        top_pick.head(10).to_string(index=False),
    )
    return out, top_pick, trade_date


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="全市场每日打分排序输出")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--out-dir", type=str, default="data/scans")
    args = parser.parse_args()

    full_ranked, top_pick, trade_date = run_scan(top_n=args.top_n)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = pd.Timestamp(trade_date).strftime("%Y%m%d")
    full_path = out_dir / f"scan_full_{date_str}.csv"
    top_path = out_dir / f"scan_top{args.top_n}_{date_str}.csv"
    full_ranked.to_csv(full_path, index=False, encoding="utf-8-sig")
    top_pick.to_csv(top_path, index=False, encoding="utf-8-sig")
    logger.info("已写出全量排名 %s 与Top候选 %s", full_path, top_path)
