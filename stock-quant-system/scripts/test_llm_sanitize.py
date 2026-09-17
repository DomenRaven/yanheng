"""LLM 解释文本去掉 HTML，避免 Streamlit 渲染成真实 DOM 后 removeChild。"""
from llm.explain_assistant import sanitize_explanation


def test_strips_tags_keeps_chinese() -> None:
    raw = "<div>信号显示<strong>偏强</strong></div>，通常对应较高排名。"
    out = sanitize_explanation(raw)
    assert "<" not in out
    assert "偏强" in out
    assert "较高排名" in out


if __name__ == "__main__":
    test_strips_tags_keeps_chinese()
    print("sanitize_explanation: ok")
