"""实盘表现：对 prediction_log / advice_log 快照做事后对账的纯函数。

全部函数只接 DataFrame / Series，不碰数据库；读库放在 `scripts/report_live_performance.py`。

口径（与 `research/factor_eval.py` 的 RankIC 保持一致，均用 Spearman 秩相关）：
- 远期收益：快照日收盘 → 之后第 h 个交易日收盘；超出数据末尾时截断到最后一日，
  并把真实用到的交易日数写进 ``effective``。停牌日用最近一次收盘向前填充。
- 实盘 RankIC：Spearman(pred_score, fwd_h)，附 p 值与样本数 n。
- 价格混杂诊断：Spearman(价格, fwd)、Spearman(模型名次, 价格)，以及控制 log(价格) 后的
  残差 RankIC——把 fwd、分数、log(价格) 都做百分位秩，fwd 秩与分数秩分别对价格秩做一元回归
  取残差，残差之间的 Pearson 相关即「偏 Spearman 相关」（Barra 因子纯化里对风险因子正交化
  的同一思路，见 docs/03-量化方法/01-多因子与Barra.md 第 2 步「中性化」）。
  另给价格五分位内的 RankIC，作为不依赖线性假设的旁证；它是粗控制，组内价格仍有差异，
  合成纯价格数据上只能把相关压掉一半左右，看方向和量级即可。
- 建议卡片结果：按 as_of × pool 汇总持有到数据末尾的收益、±8% 命中率（期末与路径两种），
  对照沪深300 / 中证1000 / 同池等权。
- 独立窗口数：快照日之间至少隔 window 个交易日才算一个独立观测，用贪心法数。
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

import numpy as np
import pandas as pd
from scipy import stats

HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
PRIMARY_HORIZON = 10
TAKE_PROFIT = 0.08
STOP_LOSS = -0.08
INDEX_HS300 = "sh000300"
INDEX_CSI1000 = "sh000852"


class ForwardReturns(NamedTuple):
    frame: pd.DataFrame            # index=symbol; price_asof, fwd_<h>, real_<h>（目标日有真实行情）
    effective: dict[int, int]      # 名义 h → 实际用到的交易日数（截断后）
    coverage: dict[int, float]     # 名义 h → 目标日有真实行情的股票占比（<1 说明有股票行情没更新/停牌）
    days_after: int                # 快照日之后数据里还有多少个交易日
    end_date: pd.Timestamp         # 数据末尾交易日


def _wide_close(quotes: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """长表 (symbol, trade_date, close) → (向前填充后的宽表, 原始宽表)。

    原始宽表用来判断目标日是否真有行情：北交所 2026-09-24 之后只更新了十几只股票，
    如果只看填充后的价格，会把「行情没更新」误当成「股价没动」。
    """
    q = quotes[["symbol", "trade_date", "close"]].copy()
    q["trade_date"] = pd.to_datetime(q["trade_date"])
    q["symbol"] = q["symbol"].astype(str)
    raw = q.pivot_table(index="trade_date", columns="symbol", values="close", aggfunc="last").sort_index()
    return raw.ffill(), raw


def trading_dates(quotes: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(pd.to_datetime(quotes["trade_date"]).unique())


def forward_returns(
    quotes: pd.DataFrame,
    as_of,
    horizons: Iterable[int] = HORIZONS,
) -> ForwardReturns:
    """快照日收盘 → 之后 h 个交易日收盘的收益；h 超出数据末尾时截断到最后一日。"""
    px, raw = _wide_close(quotes)
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts not in px.index:
        raise ValueError(f"快照日 {as_of_ts.date()} 不在行情日期里")
    dates = list(px.index)
    i0 = dates.index(as_of_ts)
    last = len(dates) - 1
    base = px.iloc[i0]
    has_base = base.notna()
    out = pd.DataFrame({"price_asof": base})
    eff: dict[int, int] = {}
    cov: dict[int, float] = {}
    for h in horizons:
        j = min(i0 + int(h), last)
        eff[int(h)] = j - i0
        if j - i0 <= 0:
            out[f"fwd_{h}"] = np.nan
            out[f"real_{h}"] = False
            cov[int(h)] = 0.0
        else:
            out[f"fwd_{h}"] = px.iloc[j] / base - 1.0
            real = raw.iloc[j].notna()
            out[f"real_{h}"] = real
            cov[int(h)] = float(real[has_base].mean()) if has_base.any() else 0.0
    out.index.name = "symbol"
    return ForwardReturns(out, eff, cov, last - i0, dates[last])


def period_returns(quotes: pd.DataFrame, start, end) -> pd.DataFrame:
    """start 收盘 → end 收盘，附路径最高/最低收盘相对起点的涨跌（用于 ±8% 是否触及）。

    real_end=False 表示 end 当天没有真实行情（停牌或行情没更新），收益是用前值填出来的。
    """
    px, raw = _wide_close(quotes)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if s not in px.index or e not in px.index or e <= s:
        return pd.DataFrame(columns=["price_start", "ret", "path_max", "path_min", "real_end"])
    base = px.loc[s]
    window = px.loc[s:e].iloc[1:]
    out = pd.DataFrame(
        {
            "price_start": base,
            "ret": px.loc[e] / base - 1.0,
            "path_max": window.max() / base - 1.0,
            "path_min": window.min() / base - 1.0,
            "real_end": raw.loc[e].notna(),
        }
    )
    out.index.name = "symbol"
    return out.dropna(subset=["price_start", "ret"])


def _aligned(*series: pd.Series) -> pd.DataFrame:
    df = pd.concat(series, axis=1)
    df.columns = range(len(series))
    return df.replace([np.inf, -np.inf], np.nan).dropna()


def rank_ic(score: pd.Series, fwd: pd.Series) -> dict:
    """Spearman(score, fwd) 与 p 值、样本数。样本不足或常数列时返回 NaN。"""
    df = _aligned(score, fwd)
    n = int(len(df))
    if n < 3 or df[0].nunique() < 2 or df[1].nunique() < 2:
        return {"rank_ic": float("nan"), "p_value": float("nan"), "n": n}
    r, p = stats.spearmanr(df[0], df[1])
    return {"rank_ic": float(r), "p_value": float(p), "n": n}


def _residual_on(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    slope, intercept = np.polyfit(x, y, 1)
    return y - (slope * x + intercept)


def _pearson_with_p(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    n = len(a)
    if n < 4 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan"), float("nan")
    r = float(np.corrcoef(a, b)[0, 1])
    r = max(min(r, 0.999999), -0.999999)
    t = r * np.sqrt((n - 2) / (1.0 - r * r))
    p = float(2.0 * stats.t.sf(abs(t), df=n - 2))
    return r, p


def price_confound(
    score: pd.Series,
    fwd: pd.Series,
    price: pd.Series,
    rank: pd.Series | None = None,
    n_bins: int = 5,
) -> dict:
    """价格水平混杂诊断。

    返回：spearman_price_fwd / spearman_rank_price（名次 1 = 最好）/ residual_rank_ic
    （控制 log 价格后的偏 Spearman）/ quintile_rank_ic（价格五分位内 RankIC 列表及均值）。
    """
    if rank is None:
        rank = score.rank(ascending=False, method="average")
    df = _aligned(score, fwd, price, rank)
    df.columns = ["score", "fwd", "price", "rank"]
    df = df[df["price"] > 0]
    n = int(len(df))
    out: dict = {"n": n}
    if n < 10:
        out.update(
            spearman_price_fwd=float("nan"), p_price_fwd=float("nan"),
            spearman_rank_price=float("nan"), p_rank_price=float("nan"),
            raw_rank_ic=float("nan"), residual_rank_ic=float("nan"), p_residual=float("nan"),
            quintile_rank_ic=[], quintile_mean_rank_ic=float("nan"),
        )
        return out
    r1, p1 = stats.spearmanr(df["price"], df["fwd"])
    r2, p2 = stats.spearmanr(df["rank"], df["price"])
    r0, _p0 = stats.spearmanr(df["score"], df["fwd"])
    # 偏 Spearman：三个变量都先取百分位秩再做线性回归取残差。控制变量若直接用 log 价格原值，
    # 秩变量对它是 S 形非线性关系，线性残差会残留同一段弯曲、造成虚假正相关
    # （合成「纯价格效应」数据上实测残差 RankIC 0.075 而非 0），所以这里用 log 价格的秩。
    z = np.log(df["price"]).rank(pct=True).to_numpy()
    y = df["fwd"].rank(pct=True).to_numpy()
    x = df["score"].rank(pct=True).to_numpy()
    res_y = _residual_on(y, z)
    res_x = _residual_on(x, z)
    r_res, p_res = _pearson_with_p(res_x, res_y)
    out.update(
        spearman_price_fwd=float(r1), p_price_fwd=float(p1),
        spearman_rank_price=float(r2), p_rank_price=float(p2),
        raw_rank_ic=float(r0), residual_rank_ic=r_res, p_residual=p_res,
    )
    bins: list[dict] = []
    try:
        q = pd.qcut(df["price"], n_bins, labels=False, duplicates="drop")
    except ValueError:
        q = pd.Series(np.zeros(n, dtype=int), index=df.index)
    for b, grp in df.groupby(q):
        ic = rank_ic(grp["score"], grp["fwd"])
        bins.append(
            {
                "quintile": int(b) + 1,
                "n": ic["n"],
                "mean_price": float(grp["price"].mean()),
                "rank_ic": ic["rank_ic"],
                "p_value": ic["p_value"],
            }
        )
    vals = [b["rank_ic"] for b in bins if not np.isnan(b["rank_ic"])]
    out["quintile_rank_ic"] = bins
    out["quintile_mean_rank_ic"] = float(np.mean(vals)) if vals else float("nan")
    return out


def decile_table(
    score: pd.Series,
    fwd: pd.Series,
    price: pd.Series | None = None,
    n_deciles: int = 10,
) -> pd.DataFrame:
    """按模型分数分十档（10 = 分数最高档），给每档样本数、远期收益均值/中位数、均价。"""
    cols = [score, fwd] + ([price] if price is not None else [])
    df = _aligned(*cols)
    df.columns = ["score", "fwd"] + (["price"] if price is not None else [])
    if len(df) < n_deciles * 2:
        return pd.DataFrame(columns=["decile", "n", "mean_fwd", "median_fwd", "mean_price"])
    df["decile"] = pd.qcut(df["score"].rank(method="first"), n_deciles, labels=False) + 1
    agg = df.groupby("decile").agg(
        n=("fwd", "size"),
        mean_fwd=("fwd", "mean"),
        median_fwd=("fwd", "median"),
        hit_tp=("fwd", lambda s: float((s >= TAKE_PROFIT).mean())),
        hit_sl=("fwd", lambda s: float((s <= STOP_LOSS).mean())),
    )
    if price is not None:
        agg["mean_price"] = df.groupby("decile")["price"].mean()
    else:
        agg["mean_price"] = np.nan
    return agg.reset_index()


def _pool_of_exchange(exchange: str) -> str:
    return "bj" if str(exchange).lower() == "bj" else "hs"


def _index_return(index_quotes: pd.DataFrame, code: str, start, end) -> float:
    sub = index_quotes[index_quotes["index_code"] == code].copy()
    if sub.empty:
        return float("nan")
    sub["trade_date"] = pd.to_datetime(sub["trade_date"])
    s = sub.set_index("trade_date")["close"].sort_index()
    s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
    if s_ts not in s.index or e_ts not in s.index:
        return float("nan")
    return float(s.loc[e_ts] / s.loc[s_ts] - 1.0)


def advice_outcomes(
    cards: pd.DataFrame,
    quotes: pd.DataFrame,
    index_quotes: pd.DataFrame,
    universe: pd.DataFrame,
    end=None,
    actions: Iterable[str] = ("open",),
    take_profit: float = TAKE_PROFIT,
    stop_loss: float = STOP_LOSS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """建议卡片事后结果。

    cards 列：as_of, pool_id, symbol, action（可选 name）。
    返回 (汇总表 按 as_of×pool_id, 明细表 每张卡)。
    收益口径：as_of 收盘 → end 收盘（默认数据末尾）；hit_* 看期末，touched_* 看路径收盘。
    end 当天没有真实行情的卡片计入 n_stale，不进均值；同池等权同样只用有真实行情的股票。
    """
    dates = trading_dates(quotes)
    end_ts = pd.Timestamp(end) if end is not None else dates[-1]
    uni = universe[["symbol", "exchange"]].copy()
    uni["symbol"] = uni["symbol"].astype(str)
    uni["pool"] = uni["exchange"].map(_pool_of_exchange)
    pool_members = {p: set(g["symbol"]) for p, g in uni.groupby("pool")}
    pool_members["all"] = set(uni["symbol"])

    sel = cards[cards["action"].isin(list(actions))].copy()
    sel["symbol"] = sel["symbol"].astype(str)
    sel["as_of"] = pd.to_datetime(sel["as_of"])
    sel["pool_id"] = sel["pool_id"].fillna("hs").astype(str)

    summary_rows: list[dict] = []
    detail_frames: list[pd.DataFrame] = []
    for (as_of, pool), grp in sel.groupby(["as_of", "pool_id"], sort=True):
        if as_of not in dates:
            continue
        i0 = dates.index(as_of)
        days = dates.index(end_ts) - i0 if end_ts in dates else 0
        pr = period_returns(quotes, as_of, end_ts)
        row = {
            "as_of": as_of.date().isoformat(),
            "pool_id": pool,
            "n_cards": int(len(grp)),
            "days_held": int(days),
            "end": end_ts.date().isoformat(),
        }
        if pr.empty or days <= 0:
            row.update(
                n_priced=0, n_stale=0, mean_ret=np.nan, median_ret=np.nan,
                hit_tp=np.nan, hit_sl=np.nan, touched_tp=np.nan, touched_sl=np.nan,
                hs300_ret=np.nan, csi1000_ret=np.nan, ew_pool_ret=np.nan,
                excess_vs_hs300=np.nan, excess_vs_ew=np.nan,
            )
            summary_rows.append(row)
            continue
        det = grp.merge(pr, left_on="symbol", right_index=True, how="left")
        det["hit_tp"] = det["ret"] >= take_profit
        det["hit_sl"] = det["ret"] <= stop_loss
        det["touched_tp"] = det["path_max"] >= take_profit
        det["touched_sl"] = det["path_min"] <= stop_loss
        priced = det.dropna(subset=["ret"])
        priced = priced[priced["real_end"].fillna(False).astype(bool)]
        members = pool_members.get(pool, pool_members["all"])
        ew = pr.loc[pr.index.isin(members) & pr["real_end"].astype(bool), "ret"]
        hs300 = _index_return(index_quotes, INDEX_HS300, as_of, end_ts)
        csi1000 = _index_return(index_quotes, INDEX_CSI1000, as_of, end_ts)
        mean_ret = float(priced["ret"].mean()) if len(priced) else np.nan
        row.update(
            n_priced=int(len(priced)),
            n_stale=int(len(det) - len(priced)),
            mean_ret=mean_ret,
            median_ret=float(priced["ret"].median()) if len(priced) else np.nan,
            hit_tp=float(priced["hit_tp"].mean()) if len(priced) else np.nan,
            hit_sl=float(priced["hit_sl"].mean()) if len(priced) else np.nan,
            touched_tp=float(priced["touched_tp"].mean()) if len(priced) else np.nan,
            touched_sl=float(priced["touched_sl"].mean()) if len(priced) else np.nan,
            hs300_ret=hs300,
            csi1000_ret=csi1000,
            ew_pool_ret=float(ew.mean()) if len(ew) else np.nan,
        )
        row["excess_vs_hs300"] = mean_ret - hs300 if not np.isnan(hs300) else np.nan
        row["excess_vs_ew"] = mean_ret - row["ew_pool_ret"] if not np.isnan(row["ew_pool_ret"]) else np.nan
        summary_rows.append(row)
        det["as_of"] = as_of.date().isoformat()
        det["end"] = end_ts.date().isoformat()
        detail_frames.append(det)

    summary = pd.DataFrame(summary_rows)
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    return summary, detail


def independent_windows(snapshot_dates: Iterable, all_trading_dates: Iterable, window: int = PRIMARY_HORIZON) -> dict:
    """快照日之间至少隔 window 个交易日才算独立观测（贪心取法）。

    n_complete 只数「之后还有满 window 个交易日数据」的窗口。
    """
    dates = sorted(pd.to_datetime(pd.Index(list(all_trading_dates))).unique())
    pos = {d: i for i, d in enumerate(dates)}
    snaps = sorted({pd.Timestamp(d) for d in snapshot_dates if pd.Timestamp(d) in pos})
    chosen: list[pd.Timestamp] = []
    last_idx = -10**9
    for d in snaps:
        if pos[d] >= last_idx + window:
            chosen.append(d)
            last_idx = pos[d]
    n_complete = sum(1 for d in chosen if pos[d] + window <= len(dates) - 1)
    return {
        "window_days": int(window),
        "n_snapshot_dates": len(snaps),
        "n_independent": len(chosen),
        "n_complete": int(n_complete),
        "chosen_dates": [d.date().isoformat() for d in chosen],
        "note": (
            f"{len(snaps)} 个快照日里，按 {window} 个交易日互不重叠只能数出 {len(chosen)} 个独立窗口，"
            f"其中 {n_complete} 个已走满 {window} 日。"
        ),
    }
