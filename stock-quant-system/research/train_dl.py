"""
Phase 3 深度学习挑战者：行业关系图注意力模型（GAT的简化实现）。

**选型依据（对齐 `docs/03-量化方法/11-深度学习前沿架构详解.md` 的选型地图，不是拍脑袋）**：
- 本项目的特征面板是**月度截面**（127个截面日，不是长序列），TFT/PatchTST 面向的是
  "长序列/多变量联合建模"，用在这里性价比不高，且文档明确警示了TFT训练早期梯度塌陷的
  已知陷阱；GNN关系建模天然契合"截面 + 关系图"的数据形态，选它。
- 关系图：用 `research/panel.py` 已经point-in-time对齐好的 `industry_code`（申万行业
  分类，覆盖99.2%截面行）构建"同行业"边——不需要额外抓新数据源，复用存量。
- 工程实现：没有引入 `torch_geometric`（会带来额外的CUDA版本对齐/编译复杂度，投入产出比
  在当前数据规模下不划算），而是用一个数学等价但更简单的写法——把"同行业"关系表示成一个
  **块对角注意力掩码**，用 `torch.nn.functional.scaled_dot_product_attention` 对
  当天全市场截面做一次带掩码的多头自注意力：掩码只允许同行业（或自身）的节点互相"看到"，
  效果等价于对每个行业子图分别做全连接图注意力（GAT在稠密子图上的特例），但只需一次批量
  矩阵运算，不需要逐行业循环，在5000+节点规模下更快。**如实说明**：这是一个简化的、
  自实现的图注意力机制，不是标准GAT/GraphSAGE库的直接调用，作为Phase3"轻量GNN挑战者"的
  一种可行实现，而不是对某个具体论文架构的精确复现。

**验证协议**：与 `research/train_lightgbm.py` 用完全相同的Walk-Forward切分
（`research.validation.walk_forward_splits`）、完全相同的三重障碍标签、完全相同的
样本外评估函数（`evaluate_fold`，Spearman RankIC），确保"严格同协议对比"（对齐计划文档
Phase3验收条款），不是自己另设一套更宽松的评价口径。

**显存约束**：本机 RTX 4060 Laptop 8GB显存（已用 `nvidia-smi` 实测确认，见Phase3验收报告），
单日截面最多约5300个节点，多头注意力分数矩阵峰值显存 <1GB，远低于约束上限，不需要邻居采样
降规模。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from research.factor_eval import FACTOR_COLS
from research.train_lightgbm import (
    _date_level_splits,
    attach_triple_barrier_labels,
    evaluate_fold,
)

logger = logging.getLogger("research.train_dl")

_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class IndustryGraphAttention(nn.Module):
    """行业关系图注意力打分模型。输入：某一天全市场截面的因子特征 + 行业分组id，
    输出：每只股票的排序分数。"""

    def __init__(self, n_features: int, hidden_dim: int = 32, n_heads: int = 4, dropout: float = 0.1):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.n_heads = n_heads
        self.input_proj = nn.Linear(n_features, hidden_dim)
        self.q_proj = nn.Linear(hidden_dim, hidden_dim)
        self.k_proj = nn.Linear(hidden_dim, hidden_dim)
        self.v_proj = nn.Linear(hidden_dim, hidden_dim)
        self.out_proj = nn.Linear(hidden_dim, hidden_dim)
        self.norm1 = nn.LayerNorm(hidden_dim)
        self.norm2 = nn.LayerNorm(hidden_dim)
        self.ffn = nn.Sequential(nn.Linear(hidden_dim, hidden_dim * 2), nn.ReLU(), nn.Linear(hidden_dim * 2, hidden_dim))
        self.score_head = nn.Linear(hidden_dim, 1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor, attn_mask: torch.Tensor) -> torch.Tensor:
        """x: (N, n_features)；attn_mask: (N, N) bool，True=允许关注（同行业或自身）。"""
        n = x.shape[0]
        h = self.input_proj(x)  # (N, hidden)

        q = self.q_proj(h).view(n, self.n_heads, self.hidden_dim // self.n_heads).transpose(0, 1)
        k = self.k_proj(h).view(n, self.n_heads, self.hidden_dim // self.n_heads).transpose(0, 1)
        v = self.v_proj(h).view(n, self.n_heads, self.hidden_dim // self.n_heads).transpose(0, 1)
        # scaled_dot_product_attention 的 attn_mask: True 位置代表"允许"参与attention
        attn_out = F.scaled_dot_product_attention(
            q.unsqueeze(0), k.unsqueeze(0), v.unsqueeze(0), attn_mask=attn_mask.unsqueeze(0).unsqueeze(0)
        ).squeeze(0)
        attn_out = attn_out.transpose(0, 1).reshape(n, self.hidden_dim)
        h = self.norm1(h + self.dropout(self.out_proj(attn_out)))
        h = self.norm2(h + self.dropout(self.ffn(h)))
        return self.score_head(h).squeeze(-1)


def _build_industry_mask(industry_codes: pd.Series, max_group_size: int = 400) -> torch.Tensor:
    """块对角掩码：同行业(或自身)=True。行业分组过大(如"未知/NaN"落在同一组)会退化成
    近似全连接、显存/计算量失控，因此超过 max_group_size 的组不建立组内边，只保留自环
    （对应现实里"分类未知的股票之间不构成真正的行业关系"，是合理的降级而不是任意截断）。"""
    codes = industry_codes.fillna("__unknown__").to_numpy()
    n = len(codes)
    same = codes[:, None] == codes[None, :]
    # 大组降级为只保留自环
    _, counts = np.unique(codes, return_counts=True)
    oversized = {c for c, cnt in zip(np.unique(codes), counts) if cnt > max_group_size}
    if oversized:
        is_oversized = np.isin(codes, list(oversized))
        big_block = np.outer(is_oversized, is_oversized)
        same = same & ~big_block
    np.fill_diagonal(same, True)
    return torch.from_numpy(same)


def _standardize(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        s = out[c].clip(out[c].quantile(0.01), out[c].quantile(0.99))
        mean, std = s.mean(), s.std()
        out[c] = (s - mean) / std if std and pd.notna(std) and std > 1e-8 else 0.0
    return out


def _pairwise_ranking_loss(scores: torch.Tensor, relevance: torch.Tensor, n_pairs: int = 2000) -> torch.Tensor:
    """RankNet风格pairwise损失：随机采样relevance不同的对，鼓励高相关性样本分数更高。
    不用全量O(N^2)对，采样控制计算量。"""
    n = scores.shape[0]
    if n < 2:
        return torch.tensor(0.0, device=scores.device, requires_grad=True)
    idx_a = torch.randint(0, n, (n_pairs,), device=scores.device)
    idx_b = torch.randint(0, n, (n_pairs,), device=scores.device)
    rel_a, rel_b = relevance[idx_a], relevance[idx_b]
    valid = rel_a != rel_b
    if valid.sum() == 0:
        return torch.tensor(0.0, device=scores.device, requires_grad=True)
    diff = scores[idx_a[valid]] - scores[idx_b[valid]]
    target = torch.sign(rel_a[valid] - rel_b[valid]).float()
    return F.binary_cross_entropy_with_logits(diff, (target + 1) / 2)


def _train_one_fold_dl(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    n_epochs: int = 15,
    lr: float = 1e-3,
    seed: int = 42,
) -> tuple[IndustryGraphAttention, pd.DataFrame]:
    torch.manual_seed(seed)
    model = IndustryGraphAttention(n_features=len(feature_cols)).to(_DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)

    train_dates = sorted(train_df["trade_date"].unique())
    for epoch in range(n_epochs):
        model.train()
        rng = np.random.default_rng(seed + epoch)
        order = rng.permutation(len(train_dates))
        epoch_loss = 0.0
        for i in order:
            date = train_dates[i]
            day = train_df[train_df["trade_date"] == date]
            if len(day) < 30:
                continue
            day_std = _standardize(day, feature_cols)
            x = torch.tensor(day_std[feature_cols].fillna(0.0).to_numpy(), dtype=torch.float32, device=_DEVICE)
            relevance = torch.tensor((day["label"].to_numpy() + 1), dtype=torch.float32, device=_DEVICE)
            mask = _build_industry_mask(day["industry_code"]).to(_DEVICE)

            optimizer.zero_grad()
            scores = model(x, mask)
            loss = _pairwise_ranking_loss(scores, relevance)
            if loss.requires_grad:
                loss.backward()
                optimizer.step()
                epoch_loss += float(loss.item())
        logger.debug("epoch %d loss=%.4f", epoch, epoch_loss)

    model.eval()
    test_dates = sorted(test_df["trade_date"].unique())
    pred_frames = []
    with torch.no_grad():
        for date in test_dates:
            day = test_df[test_df["trade_date"] == date].copy()
            if day.empty:
                continue
            day_std = _standardize(day, feature_cols)
            x = torch.tensor(day_std[feature_cols].fillna(0.0).to_numpy(), dtype=torch.float32, device=_DEVICE)
            mask = _build_industry_mask(day["industry_code"]).to(_DEVICE)
            scores = model(x, mask).cpu().numpy()
            day["pred_score"] = scores
            pred_frames.append(day)
    test_pred = pd.concat(pred_frames, ignore_index=True) if pred_frames else test_df.assign(pred_score=np.nan)
    return model, test_pred


def walk_forward_oos_scores_dl(
    panel: pd.DataFrame,
    n_epochs: int = 15,
    train_size: int | None = None,
    test_size: int | None = None,
) -> pd.DataFrame:
    """与 `research.train_lightgbm.walk_forward_oos_scores` 同构的DL版本，供
    `research/backtest.py` 复用同一套回测引擎对DL挑战者做业绩评估（严格同协议对比，
    不给DL模型另开一套更宽松的回测口径）。"""
    panel = panel.copy()
    if "label" not in panel.columns:
        panel = attach_triple_barrier_labels(panel)
    panel = panel.dropna(subset=["label"])
    feature_cols = [c for c in FACTOR_COLS if c in panel.columns]

    unique_dates = np.array(sorted(panel["trade_date"].unique()))
    n_dates = len(unique_dates)
    train_size = train_size or max(24, n_dates // 3)
    test_size = test_size or max(6, n_dates // 10)

    oos_frames = []
    for train_dates, test_dates in _date_level_splits(
        unique_dates, "walk_forward", train_size=train_size, test_size=test_size, expanding=True
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)]
        test_df = panel[panel["trade_date"].isin(test_dates)]
        if train_df.empty or test_df.empty:
            continue
        _, test_pred = _train_one_fold_dl(train_df, test_df, feature_cols, n_epochs=n_epochs)
        oos_frames.append(test_pred)

    if not oos_frames:
        return pd.DataFrame()
    return pd.concat(oos_frames, ignore_index=True)


def run_dl_training(
    panel_path: str = "data/feature_panel.parquet",
    registry_dir: str = "mlops/registry",
    n_epochs: int = 15,
) -> dict:
    logger.info("使用设备: %s", _DEVICE)
    panel = pd.read_parquet(panel_path)
    from research.factor_eval import add_forward_return

    panel = add_forward_return(panel)
    panel = attach_triple_barrier_labels(panel)
    panel = panel.dropna(subset=["label"])
    feature_cols = [c for c in FACTOR_COLS if c in panel.columns]

    unique_dates = np.array(sorted(panel["trade_date"].unique()))
    logger.info("面板截面日数: %d，总行数: %d，特征: %s", len(unique_dates), len(panel), feature_cols)

    n_dates = len(unique_dates)
    train_size = max(24, n_dates // 3)
    test_size = max(6, n_dates // 10)

    fold_results = []
    for train_dates, test_dates in _date_level_splits(
        unique_dates, "walk_forward", train_size=train_size, test_size=test_size, expanding=True
    ):
        train_df = panel[panel["trade_date"].isin(train_dates)]
        test_df = panel[panel["trade_date"].isin(test_dates)]
        if train_df.empty or test_df.empty:
            continue
        _, test_pred = _train_one_fold_dl(train_df, test_df, feature_cols, n_epochs=n_epochs)
        fold_results.append(evaluate_fold(test_pred))
        logger.info("walk-forward fold 完成: %s", fold_results[-1])

    valid = [f for f in fold_results if f.get("n_periods", 0) > 0]
    walk_forward_oos = (
        {
            "n_folds": len(valid),
            "rank_ic_mean": round(float(np.mean([f["rank_ic_mean"] for f in valid])), 4),
            "pct_positive_mean": round(float(np.mean([f["pct_positive"] for f in valid])), 3),
        }
        if valid
        else {"n_folds": 0}
    )

    # 最终生产模型：用全部数据训练一次（walk-forward只用来估计样本外表现，与train_lightgbm一致）
    final_model = IndustryGraphAttention(n_features=len(feature_cols)).to(_DEVICE)
    optimizer = torch.optim.Adam(final_model.parameters(), lr=1e-3, weight_decay=1e-5)
    all_dates = sorted(panel["trade_date"].unique())
    for epoch in range(n_epochs):
        rng = np.random.default_rng(100 + epoch)
        for i in rng.permutation(len(all_dates)):
            day = panel[panel["trade_date"] == all_dates[i]]
            if len(day) < 30:
                continue
            day_std = _standardize(day, feature_cols)
            x = torch.tensor(day_std[feature_cols].fillna(0.0).to_numpy(), dtype=torch.float32, device=_DEVICE)
            relevance = torch.tensor((day["label"].to_numpy() + 1), dtype=torch.float32, device=_DEVICE)
            mask = _build_industry_mask(day["industry_code"]).to(_DEVICE)
            optimizer.zero_grad()
            loss = _pairwise_ranking_loss(final_model(x, mask), relevance)
            if loss.requires_grad:
                loss.backward()
                optimizer.step()

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_dl"
    run_dir = Path(registry_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    torch.save(final_model.state_dict(), run_dir / "model_dl.pt")
    with open(run_dir / "model.pkl", "wb") as f:
        pickle.dump({"architecture": "IndustryGraphAttention", "state_dict_path": "model_dl.pt",
                     "n_features": len(feature_cols)}, f)

    metadata = {
        "run_id": run_id,
        "model_type": "dl_industry_graph_attention",
        "trained_at": dt.datetime.now().isoformat(),
        "device": str(_DEVICE),
        "feature_cols": feature_cols,
        "n_rows_total": len(panel),
        "n_dates_total": n_dates,
        "date_range": [str(unique_dates.min()), str(unique_dates.max())],
        "n_epochs": n_epochs,
        "walk_forward_oos": walk_forward_oos,
        "walk_forward_fold_detail": fold_results,
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    logger.info("DL挑战者训练完成 run_id=%s，walk_forward_oos=%s", run_id, walk_forward_oos)
    return metadata


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Phase3 行业图注意力DL挑战者训练")
    parser.add_argument("--panel", type=str, default="data/feature_panel.parquet")
    parser.add_argument("--registry-dir", type=str, default="mlops/registry")
    parser.add_argument("--epochs", type=int, default=15)
    args = parser.parse_args()

    result = run_dl_training(args.panel, args.registry_dir, args.epochs)
    print(json.dumps(result["walk_forward_oos"], ensure_ascii=False, indent=2))
