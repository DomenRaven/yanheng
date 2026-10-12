"""市场状态（MR-A）：沪深300 均线 + 近20日收益 → 进攻/中性/防守/不足。

规格：`docs/requirements-market-regime-20260921.md`
理论：`docs/04-风险管理/03-场景化决策建议框架.md`（市场状态是建议的一级输入）；
`docs/03-量化方法/02-统计与时间序列.md`（只用截至 t 的价格）。
本模块只标注卡片，不改 action、不改股数、不改 risk_fraction。
月份口诀与 HMM 不在此实现。
"""
from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from common.config import get_config

logger = logging.getLogger("advice.market_regime")

_LABEL = {
    "offense": "进攻",
    "neutral": "中性",
    "defense": "防守",
    "unknown": "不足",
}


def regime_params() -> dict[str, Any]:
    cfg = get_config().get("market_regime") or {}
    return {
        "benchmark": str(cfg.get("benchmark", "sh000300")),
        "confirm_index": str(cfg.get("confirm_index", "sh000001")),
        "style_index": str(cfg.get("style_index", "sh000852")),
        "ma_window": int(cfg.get("ma_window", 20)),
        "offense_ret": float(cfg.get("offense_ret", 0.03)),
        "defense_ret": float(cfg.get("defense_ret", -0.03)),
        "lookback_rows": int(cfg.get("lookback_rows", 80)),
    }


def classify_closes(
    closes: list[float],
    *,
    ma_window: int = 20,
    offense_ret: float = 0.03,
    defense_ret: float = -0.03,
) -> dict[str, Any]:
    """用已排序（旧→新）的收盘序列打档。不足 ma_window+1 根 → unknown。"""
    clean = [float(x) for x in closes if x is not None and not (isinstance(x, float) and math.isnan(x)) and float(x) > 0]
    need = ma_window + 1
    if len(clean) < need:
        return {
            "regime": "unknown",
            "ret_n": None,
            "above_ma": None,
            "ma": None,
            "last_close": clean[-1] if clean else None,
            "n_obs": len(clean),
        }
    window = clean[-ma_window:]
    ma = sum(window) / ma_window
    last = clean[-1]
    prev = clean[-(ma_window + 1)]
    ret_n = last / prev - 1.0
    above = last >= ma
    if above and ret_n >= offense_ret:
        regime = "offense"
    elif (not above) and ret_n <= defense_ret:
        regime = "defense"
    else:
        regime = "neutral"
    return {
        "regime": regime,
        "ret_n": ret_n,
        "above_ma": above,
        "ma": ma,
        "last_close": last,
        "n_obs": len(clean),
    }


def _load_closes(conn, index_code: str, limit: int) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT trade_date, close
        FROM index_quotes
        WHERE index_code = ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [index_code, int(limit)],
    ).df()


def _as_closes(df: pd.DataFrame) -> tuple[list[float], str | None]:
    if df is None or df.empty:
        return [], None
    ordered = df.iloc[::-1]
    closes = [float(x) for x in ordered["close"].tolist() if pd.notna(x)]
    as_of = ordered["trade_date"].iloc[-1]
    return closes, str(as_of)[:10]


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1%}"


def _advice(regime: str) -> str:
    if regime == "offense":
        return "可以按卡片上的股数建仓。"
    if regime == "defense":
        return "新开仓少做，已持有的先盯止损价。"
    return "按卡片上的股数和止损做。"


def _card_note(regime: str, ret_n: float | None) -> str:
    advice = _advice(regime)
    pct = _fmt_pct(ret_n)
    if regime == "offense":
        return f"【市场】沪深300进攻档，近20个交易日{pct}，在20日均线之上。{advice}"
    if regime == "defense":
        return f"【市场】沪深300防守档，近20个交易日{pct}，在20日均线之下。{advice}"
    if regime == "neutral":
        return f"【市场】沪深300中性档，近20个交易日{pct}。{advice}"
    return f"【市场】沪深300日线不够，天气先不算。{advice}"


def _empty_regime(reason: str) -> dict[str, Any]:
    return {
        "regime": "unknown",
        "label": _LABEL["unknown"],
        "benchmark": "sh000300",
        "benchmark_name": "沪深300",
        "as_of": None,
        "ret_n": None,
        "above_ma": None,
        "last_close": None,
        "confirm_name": "上证指数",
        "confirm_ret_n": None,
        "confirm_agrees": None,
        "style_spread": None,
        "headline": reason,
        "card_note": _card_note("unknown", None),
        "reason_detail": reason,
        "advice": _advice("unknown"),
        "risk_line": None,
        "error": reason,
    }


def load_market_regime(conn) -> dict[str, Any]:
    """从已有连接读取指数（调用方负责只读/写连接）。失败时返回 unknown，不抛给页面。"""
    p = regime_params()
    try:
        bench_df = _load_closes(conn, p["benchmark"], p["lookback_rows"])
    except Exception as exc:
        logger.warning("读取基准指数失败: %s", exc)
        return _empty_regime("指数日线暂时读不到（可能正在更新数据）。未判断趋势。")

    closes, as_of = _as_closes(bench_df)
    core = classify_closes(
        closes,
        ma_window=p["ma_window"],
        offense_ret=p["offense_ret"],
        defense_ret=p["defense_ret"],
    )
    regime = core["regime"]
    ret_n = core["ret_n"]

    confirm_ret = None
    confirm_above = None
    try:
        c_closes, _ = _as_closes(_load_closes(conn, p["confirm_index"], p["lookback_rows"]))
        c_core = classify_closes(
            c_closes,
            ma_window=p["ma_window"],
            offense_ret=p["offense_ret"],
            defense_ret=p["defense_ret"],
        )
        confirm_ret = c_core["ret_n"]
        confirm_above = c_core["above_ma"]
    except Exception as exc:
        logger.info("上证对照未读到: %s", exc)

    style_spread = None
    try:
        s_closes, _ = _as_closes(_load_closes(conn, p["style_index"], p["lookback_rows"]))
        s_core = classify_closes(
            s_closes,
            ma_window=p["ma_window"],
            offense_ret=p["offense_ret"],
            defense_ret=p["defense_ret"],
        )
        if ret_n is not None and s_core["ret_n"] is not None:
            style_spread = ret_n - s_core["ret_n"]
    except Exception as exc:
        logger.info("大小盘差未读到: %s", exc)

    agrees = None
    if core["above_ma"] is not None and confirm_above is not None:
        agrees = bool(core["above_ma"]) == bool(confirm_above)

    advice = _advice(regime)
    if regime == "unknown":
        headline = f"沪深300日线不够21根，天气先不算。{advice}"
    else:
        side = "在20日均线之上" if core["above_ma"] else "在20日均线之下"
        headline = f"沪深300{side}，近20个交易日 {_fmt_pct(ret_n)}，算{_LABEL[regime]}。{advice}"

    detail = (
        f"沪深300（{p['benchmark']}）截止{as_of or '—'}："
        f"{_LABEL[regime]}，近20日{_fmt_pct(ret_n)}，"
        f"{'在20日均线之上' if core['above_ma'] else '在20日均线之下'}。"
        f"{advice}"
    )
    return {
        "regime": regime,
        "label": _LABEL[regime],
        "benchmark": p["benchmark"],
        "benchmark_name": "沪深300",
        "as_of": as_of,
        "ret_n": ret_n,
        "above_ma": core["above_ma"],
        "last_close": core["last_close"],
        "confirm_name": "上证指数",
        "confirm_ret_n": confirm_ret,
        "confirm_agrees": agrees,
        "style_spread": style_spread,
        "headline": headline,
        "advice": advice,
        "card_note": _card_note(regime, ret_n),
        "reason_detail": detail,
        "risk_line": (
            f"沪深300在20日均线之下，近20个交易日{_fmt_pct(ret_n)}，到了防守档。"
            if regime == "defense"
            else None
        ),
        "error": None,
    }


def annotate_cards(cards: list[dict], regime: dict[str, Any]) -> list[dict]:
    """给每张卡追加市场理由与大白话。不改 action / 股数。"""
    if not cards:
        return []
    note = str(regime.get("card_note") or "")
    reason = {
        "type": "market",
        "ref": regime.get("benchmark") or "sh000300",
        "detail": regime.get("reason_detail") or note,
    }
    risk_line = regime.get("risk_line")
    out: list[dict] = []
    for card in cards:
        c = dict(card)
        reasons = list(c.get("reasons") or [])
        if not any(r.get("type") == "market" for r in reasons):
            reasons.append(reason)
        c["reasons"] = reasons
        if risk_line:
            risks = list(c.get("risks") or [])
            if risk_line not in risks:
                risks.append(risk_line)
            c["risks"] = risks
        summary = str(c.get("plain_summary") or "").rstrip()
        if note and note not in summary:
            c["plain_summary"] = f"{summary} {note}".strip() if summary else note
        out.append(c)
    return out
