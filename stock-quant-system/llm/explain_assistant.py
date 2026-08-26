"""
Phase 4 LLM解释层——"翻译层，不做主力预测"（用户原话）。

**职责边界**：本模块只做自然语言解释/摘要，绝不用LLM直接输出买卖建议或预测分数。
量化结论在 advice/ research/ behavior/ risk/ 里算好，这里只把结构化结果 + 新闻标题
译成中文，并强制附带免责声明。

**API**：默认通义千问（阿里云百炼 DashScope OpenAI 兼容接口）。`config.yaml` 的
`llm.provider` 可为 `dashscope` 或 `deepseek`。用标准库 requests，不引入 openai SDK。
Key 从 `.env` 读取，约定与 `common/tushare_client.py` 相同。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from common.config import get_config

logger = logging.getLogger("llm.explain_assistant")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PROVIDERS = {
    "dashscope": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "default_model": "qwen-plus",
        "default_key_env": "DASHSCOPE_API_KEY",
        "help_url": "https://bailian.console.aliyun.com/",
        "display_name": "通义千问",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "default_model": "deepseek-chat",
        "default_key_env": "DEEPSEEK_API_KEY",
        "help_url": "https://platform.deepseek.com/api_keys",
        "display_name": "DeepSeek",
    },
}

_SYSTEM_PROMPT = (
    "你是一个A股量化研究工具里的解释助手。你的唯一职责是把用户提供的结构化计算结果"
    "（模型打分、因子暴露、行为金融信号、风险指标、最新新闻标题）用清晰、克制的中文讲清楚"
    "\"这些数字大致意味着什么\"。严格规则：\n"
    "1. 不得给出\"建议买入/卖出/加仓/减仓\"这类指令性表述，只能说\"信号显示……，通常对应……\"；\n"
    "2. 不得编造未在输入中出现的具体数字或新闻内容；\n"
    "3. 如果输入的信号互相矛盾，必须明确指出矛盾，不要为了流畅而假装一致；\n"
    "4. 结尾必须附上一句：\"以上内容基于系统历史数据计算生成，不构成投资建议，最终决策及风险由使用者自行承担。\""
)


class LLMNotConfiguredError(RuntimeError):
    pass


def _llm_settings() -> dict:
    cfg = get_config().get("llm", {}) or {}
    provider = str(cfg.get("provider", "dashscope")).strip().lower()
    if provider not in _PROVIDERS:
        raise RuntimeError(f"不支持的 llm.provider={provider}，可选: {list(_PROVIDERS)}")
    spec = _PROVIDERS[provider]
    return {
        "provider": provider,
        "url": cfg.get("base_url") or spec["url"],
        "model": cfg.get("model") or spec["default_model"],
        "key_env": cfg.get("api_key_env") or spec["default_key_env"],
        "help_url": spec["help_url"],
        "display_name": spec["display_name"],
    }


def _load_api_key(key_env_name: str, display_name: str, help_url: str) -> str:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{key_env_name}="):
                    value = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    key = os.environ.get(key_env_name, "").strip()
    if key:
        return key
    raise LLMNotConfiguredError(
        f"未找到 {key_env_name}，请在项目根目录 .env 中配置 {key_env_name}=xxx "
        f"（{display_name} Key：{help_url}），"
        "否则本工具的AI解释功能不可用，其余结构化数据/建议卡片不受影响。"
    )


def _call_llm(messages: list[dict], temperature: float = 0.3, timeout: int = 30) -> str:
    settings = _llm_settings()
    api_key = _load_api_key(settings["key_env"], settings["display_name"], settings["help_url"])
    resp = requests.post(
        settings["url"],
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": settings["model"],
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        logger.error("LLM HTTP %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def build_context_text(
    symbol: str,
    name: str | None,
    pred_score: float | None,
    rank: int | None,
    factor_snapshot: dict | None,
    behavior_flags: dict | None,
    risk_flags: dict | None,
    news_titles: list[str] | None,
) -> str:
    """把各模块算好的结构化结果拼成一段给LLM看的上下文文本。字段缺失就跳过，不编造。"""
    lines = [f"股票：{symbol} {name or ''}".strip()]
    if pred_score is not None:
        lines.append(f"模型打分：{pred_score:.4f}（全市场排名第{rank}）" if rank else f"模型打分：{pred_score:.4f}")
    if factor_snapshot:
        factor_lines = [f"  - {k}: {v:.4f}" if isinstance(v, (int, float)) else f"  - {k}: {v}"
                         for k, v in factor_snapshot.items() if v is not None]
        if factor_lines:
            lines.append("因子暴露：\n" + "\n".join(factor_lines))
    if behavior_flags:
        behavior_lines = [f"  - {k}: {v}" for k, v in behavior_flags.items() if v]
        if behavior_lines:
            lines.append("行为金融信号：\n" + "\n".join(behavior_lines))
    if risk_flags:
        risk_lines = [f"  - {k}: {v}" for k, v in risk_flags.items() if v is not None]
        if risk_lines:
            lines.append("风险指标：\n" + "\n".join(risk_lines))
    if news_titles:
        lines.append("最新相关新闻标题（仅标题，不代表本系统verified真实性）：\n" +
                      "\n".join(f"  - {t}" for t in news_titles[:8]))
    return "\n".join(lines)


def generate_explanation(context_text: str) -> str:
    """context_text 由 build_context_text() 生成。抛出 LLMNotConfiguredError 如果没配Key，
    调用方需要捕获并优雅降级。"""
    settings = _llm_settings()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"请解释以下数据：\n\n{context_text}"},
    ]
    try:
        return _call_llm(messages)
    except LLMNotConfiguredError:
        raise
    except requests.RequestException as e:
        logger.error("调用%s API失败: %s", settings["display_name"], e)
        raise RuntimeError(f"调用{settings['display_name']} API失败（网络或额度问题）: {e}") from e


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    demo_context = build_context_text(
        symbol="000001", name="平安银行", pred_score=0.82, rank=15,
        factor_snapshot={"factor_mom_12_1": 0.05, "factor_roe": 0.12},
        behavior_flags={"disposition_flag": "浮盈较大，接近历史获利兑现区"},
        risk_flags={"max_single_weight": 0.18},
        news_titles=["平安银行发布2026年半年报", "平安银行大宗交易折价成交"],
    )
    print("=== 上下文 ===")
    print(demo_context)
    try:
        print("\n=== LLM解释 ===")
        print(generate_explanation(demo_context))
    except LLMNotConfiguredError as e:
        print(f"\n[跳过实际调用] {e}")
