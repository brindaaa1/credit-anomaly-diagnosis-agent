"""web_search 工具：MOCK 模式返回结构化样例；真实模式下网络异常要返回
{"hits": [], "error": ...} 而不是抛异常——Orchestrator.run() 是同步阻塞调用，
一个挂起/抛异常的网络请求会卡死整个诊断任务。
"""
import requests

import config
from tools.web_search import web_search


def test_web_search_mock_mode_returns_structured_hits():
    result = web_search("信贷 助贷行业 监管政策", top_k=3)
    assert result["tool_name"] == "web_search"
    assert "error" not in result
    assert len(result["hits"]) >= 1
    hit = result["hits"][0]
    assert set(hit.keys()) == {"title", "url", "snippet", "published_date"}


def test_web_search_mock_mode_respects_top_k():
    result = web_search("信贷 助贷行业 监管政策", top_k=1)
    assert len(result["hits"]) == 1


def test_web_search_real_mode_parses_successful_response(monkeypatch):
    monkeypatch.setattr(config, "MOCK_WEB_SEARCH", False)
    monkeypatch.setattr(config, "SERPAPI_API_KEY", "fake-key")

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"organic_results": [
                {"title": "标题A", "link": "https://a.example", "snippet": "正文摘要A",
                 "date": "2026-07-01"},
                {"title": "标题B", "link": "https://b.example", "snippet": "正文摘要B",
                 "date": "2026-07-02"},
            ]}

    def fake_get(url, params, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    result = web_search("测试查询", top_k=2)

    assert captured["url"] == "https://serpapi.com/search.json"
    assert captured["timeout"] == config.WEB_SEARCH_TIMEOUT
    assert captured["params"]["api_key"] == "fake-key"
    assert captured["params"]["q"] == "测试查询"
    assert captured["params"]["engine"] == "google"
    assert result["hits"][0] == {
        "title": "标题A", "url": "https://a.example",
        "snippet": "正文摘要A", "published_date": "2026-07-01",
    }


def test_web_search_real_mode_network_error_returns_error_dict(monkeypatch):
    monkeypatch.setattr(config, "MOCK_WEB_SEARCH", False)

    def fake_get(url, params, timeout):
        raise requests.exceptions.Timeout("connection timed out")

    monkeypatch.setattr(requests, "get", fake_get)

    result = web_search("测试查询", top_k=3)

    assert result["tool_name"] == "web_search"
    assert result["hits"] == []
    assert "error" in result


def test_web_search_real_mode_malformed_response_returns_error_dict(monkeypatch):
    monkeypatch.setattr(config, "MOCK_WEB_SEARCH", False)
    monkeypatch.setattr(config, "SERPAPI_API_KEY", "fake-key")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            # Missing "title" key in result
            return {"organic_results": [
                {"link": "https://a.example", "snippet": "正文摘要A",
                 "date": "2026-07-01"},
            ]}

    def fake_get(url, params, timeout):
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    result = web_search("测试查询", top_k=1)

    assert result["tool_name"] == "web_search"
    assert result["hits"] == []
    assert "error" in result


def test_web_search_real_mode_api_error_in_200_response_returns_error_dict(monkeypatch):
    monkeypatch.setattr(config, "MOCK_WEB_SEARCH", False)
    monkeypatch.setattr(config, "SERPAPI_API_KEY", "bad-key")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "Invalid API key."}

    def fake_get(url, params, timeout):
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    result = web_search("测试查询", top_k=3)

    assert result["tool_name"] == "web_search"
    assert result["hits"] == []
    assert result["error"] == "Invalid API key."
