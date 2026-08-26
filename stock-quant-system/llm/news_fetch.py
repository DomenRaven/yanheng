"""
个股新闻抓取（东方财富搜索接口），供 `llm/explain_assistant.py` 的"联网资讯"输入。

**为什么不直接调用 akshare.stock_news_em()（如实说明，对齐vibe coding八荣八耻第7条）**：
已用真实请求验证（2026-08-26），akshare 当前安装版本(news/news_stock.py)在东财接口本身
正常返回数据之后，内部有一处后处理 bug——用 `str.replace(r"\\u3000", "", regex=True)`
去除全角空格，但本环境 pandas 的 pyarrow-backed 字符串后端把 `\\u3000` 当正则里的非法
转义序列直接抛 `pyarrow.lib.ArrowInvalid`，导致函数在拿到真实数据后崩在这一步。这是
akshare库自身的兼容性bug，不是接口本身不可用，因此不是"重新发明轮子"，而是复用已验证
可用的URL/请求参数/JSON解析逻辑，只是把最后一步全角空格清理换成非正则的字面量替换
（`str.replace(chr(0x3000), "")`，不触发pyarrow的正则校验）。

**为什么放在 llm/ 而不是 ingestion/（分层说明）**：`ingestion/` 现有模块都是"批量回补 +
sync_log断点续传 + 落库point-in-time历史表"的模式，服务于因子计算等需要历史一致性的场景。
这里是"给LLM解释层实时查最近新闻"的按需查询，不落库存历史（新闻文本没有point-in-time
对齐的需求，每次都查最新的即可），语义和现有ingestion/模式不同，不适合套用sync_*框架，
因此单独放在 llm/ 包内作为"资讯输入适配器"。
"""
from __future__ import annotations

import json
import logging

import pandas as pd
from curl_cffi import requests

logger = logging.getLogger("llm.news_fetch")

_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"


def fetch_stock_news(symbol: str, page_size: int = 10) -> pd.DataFrame:
    """东方财富个股新闻搜索。symbol 不带交易所后缀（如 "000001"）。
    返回列：title, content, publish_time, source, url。"""
    inner_param = {
        "uid": "",
        "keyword": symbol,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    params = {
        "cb": "jQuery_stockquant_news",
        "param": json.dumps(inner_param, ensure_ascii=False),
        "_": "1",
    }
    headers = {
        "accept": "*/*",
        "referer": f"https://so.eastmoney.com/news/s?keyword={symbol}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        resp = requests.get(_SEARCH_URL, params=params, headers=headers, timeout=10)
        text = resp.text.strip()
        json_str = text[text.index("(") + 1 : text.rindex(")")]
        data = json.loads(json_str)
        records = data.get("result", {}).get("cmsArticleWebOld", [])
    except Exception as e:
        logger.warning("抓取个股新闻失败 symbol=%s: %s: %s", symbol, type(e).__name__, e)
        return pd.DataFrame(columns=["title", "content", "publish_time", "source", "url"])

    if not records:
        return pd.DataFrame(columns=["title", "content", "publish_time", "source", "url"])

    df = pd.DataFrame(records)
    df["url"] = "http://finance.eastmoney.com/a/" + df["code"].astype(str) + ".html"
    out = df.rename(columns={"date": "publish_time", "mediaName": "source", "title": "title", "content": "content"})

    def _clean(s: pd.Series) -> pd.Series:
        return (
            s.astype(str)
            .str.replace("<em>", "", regex=False)
            .str.replace("</em>", "", regex=False)
            .str.replace(chr(0x3000), "", regex=False)
            .str.replace("\r\n", " ", regex=False)
        )

    out["title"] = _clean(out["title"])
    out["content"] = _clean(out["content"])
    return out[["title", "content", "publish_time", "source", "url"]]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = fetch_stock_news("000001")
    print(f"抓到 {len(sample)} 条新闻")
    if not sample.empty:
        print(sample[["title", "publish_time", "source"]].head(5).to_string())
