"""LLM 解释层：把本机日历和新闻发布时间注入上下文，避免模型用训练记忆补年份。"""
from __future__ import annotations

import datetime as dt
import re

from llm.explain_assistant import (
    LLMNotConfiguredError,
    _system_prompt,
    build_context_text,
    generate_explanation,
)


def test_context_injects_calendar_and_news_time() -> None:
    as_of = "2026-08-26"
    text = build_context_text(
        symbol="000001",
        name="平安银行",
        pred_score=0.12,
        rank=100,
        factor_snapshot=None,
        behavior_flags=None,
        risk_flags=None,
        as_of=as_of,
        snapshot_trade_date="2026-08-26",
        news_items=[{"title": "某公司公告（08-21）", "publish_time": "2026-08-21 09:00:00"}],
    )
    assert "系统日历日期（今天）：2026-08-26" in text
    assert "模型打分对应交易日：2026-08-26" in text
    assert "[2026-08-21 09:00:00] 某公司公告（08-21）" in text
    assert "2024" not in text


def test_news_titles_fallback_still_works() -> None:
    text = build_context_text(
        symbol="000001",
        name=None,
        pred_score=None,
        rank=None,
        factor_snapshot=None,
        behavior_flags=None,
        risk_flags=None,
        news_titles=["只有标题没有时间"],
        as_of="2026-08-26",
    )
    assert "系统日历日期（今天）：2026-08-26" in text
    assert "只有标题没有时间" in text


def test_system_prompt_uses_as_of_and_does_not_prime_old_year() -> None:
    prompt = _system_prompt("2026-08-26")
    assert "2026-08-26" in prompt
    assert "2024" not in prompt
    assert "没有独立实时行情" in prompt


def test_live_explanation_does_not_claim_today_is_2024() -> None:
    """有 Key 才打真实 API。不断言解释里完全不出现 2024（历史新闻可能提到），
    只禁止把「今天/当前」说成 2024。"""
    as_of = dt.date.today().isoformat()
    context = build_context_text(
        symbol="000001",
        name="平安银行",
        pred_score=0.5,
        rank=10,
        factor_snapshot={"factor_roe": 0.1},
        behavior_flags=None,
        risk_flags=None,
        as_of=as_of,
        snapshot_trade_date=as_of,
        news_items=[{"title": "平安银行相关新闻（08-21）", "publish_time": f"{as_of} 09:00:00"}],
    )
    try:
        text = generate_explanation(context, as_of=as_of)
    except LLMNotConfiguredError:
        print("live explanation: skipped (no API key)")
        return
    assert text.strip(), "LLM 返回空文本"
    forbidden = re.compile(r"(今天|当前|现在|目前).{0,12}2024")
    assert not forbidden.search(text), f"解释把今天说成了 2024：{text[:400]}"
    print(f"live explanation: ok ({len(text)} chars, as_of={as_of})")


if __name__ == "__main__":
    test_context_injects_calendar_and_news_time()
    test_news_titles_fallback_still_works()
    test_system_prompt_uses_as_of_and_does_not_prime_old_year()
    test_live_explanation_does_not_claim_today_is_2024()
    print("test_llm_explain: ok")
