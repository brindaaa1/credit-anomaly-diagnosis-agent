"""工具4: web_search —— 外部监管/行业新闻检索（只读，调用 SerpAPI，Google 引擎）。

跟另外三个工具不同：这是第一个访问外部网络的工具，返回不可控的第三方文本。
MOCK_WEB_SEARCH=1 时读本地样例数据模拟，不发真实请求。

Orchestrator.run() 是同步阻塞调用，真实调用必须有超时兜底：网络异常/超时时
返回空 hits + error 字段，绝不抛异常，否则会卡死整个诊断任务。
"""
import json

import requests

import config


def web_search(query: str, top_k: int = 3) -> dict:
    if config.MOCK_WEB_SEARCH:
        return _mock_search(query, top_k)

    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": query,
                "api_key": config.SERPAPI_API_KEY,
                "num": top_k,
            },
            timeout=config.WEB_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return {"tool_name": "web_search", "query": query, "hits": [], "error": data["error"]}
        hits = [
            {
                "title": r["title"],
                "url": r["link"],
                "snippet": r["snippet"],
                "published_date": r.get("date"),
            }
            for r in data.get("organic_results", [])[:top_k]
        ]
    except (requests.RequestException, KeyError, TypeError, ValueError, AttributeError) as e:
        return {"tool_name": "web_search", "query": query, "hits": [], "error": str(e)}

    return {"tool_name": "web_search", "query": query, "hits": hits}


def _mock_search(query: str, top_k: int) -> dict:
    try:
        with open(config.MOCK_WEB_RESULTS_PATH, encoding="utf-8") as f:
            hits = json.load(f)
    except (OSError, ValueError) as e:
        return {"tool_name": "web_search", "query": query, "hits": [], "error": str(e)}
    return {"tool_name": "web_search", "query": query, "hits": hits[:top_k]}
