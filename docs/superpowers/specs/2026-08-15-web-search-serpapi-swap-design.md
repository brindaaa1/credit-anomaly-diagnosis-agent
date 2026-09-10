# 设计：web_search 工具从 Tavily 换成 SerpAPI（子项目 A）

## 背景

`tools/web_search.py` 目前调用 Tavily API 做外部监管/行业新闻检索。用户已经拿到 SerpAPI（Google 引擎）的 key，希望把真实调用换成 SerpAPI，同时不改动函数对外的返回契约，因为 `agent/orchestrator.py`、`regulatory_policy_change` 假设逻辑、以及 `tests/test_web_search.py` 都依赖现有的 `{tool_name, query, hits, error?}` / `hits: [{title, url, snippet, published_date}]` 结构。

MOCK 模式（`MOCK_WEB_SEARCH=1`，默认开启）完全不受影响——它读本地 `kb/mock_web_results.json`，不发真实请求，这次改动只涉及真实模式的分支。

## 目标

- 把 `tools/web_search.py` 里 `MOCK_WEB_SEARCH=0` 分支的请求逻辑从 Tavily 换成 SerpAPI（Google 引擎）。
- 对外接口（函数签名、返回结构）不变。
- 沿用现有"绝不抛异常，网络/解析异常一律兜底成 `{hits: [], error: ...}`"的原则，并补上 SerpAPI 特有的错误场景。

## 非目标

- 不引入多 provider 抽象层——这是一次性切换，不是要支持将来随时换搜索服务。
- 不加地区/语言偏向参数（`hl`/`gl`）——用户明确选择保持默认，以后需要再加。
- 不改动 MOCK 模式逻辑、`config.MAX_TOOL_CALLS` 预算相关代码、或 `regulatory_policy_change` 假设的判断逻辑。

## 设计

### 请求

`requests.get("https://serpapi.com/search.json", params={"engine": "google", "q": query, "api_key": config.SERPAPI_API_KEY, "num": top_k}, timeout=config.WEB_SEARCH_TIMEOUT)`，替换原来的 `requests.post(...)`。

### 响应解析

SerpAPI 成功响应体形如 `{"organic_results": [{"title": ..., "link": ..., "snippet": ..., "date": ...(可选)}, ...]}`。映射规则：

- `title` → `title`
- `link` → `url`
- `snippet` → `snippet`
- `date`（可能不存在）→ `published_date`，缺失时用 `.get("date")` 落地为 `None`

取前 `top_k` 条，跟现有逻辑一致。

### 错误处理

两层错误场景都要覆盖：

1. **网络层错误**（超时、连接失败、非 2xx 状态码）：沿用现有 `try/except (requests.RequestException, KeyError, TypeError, ValueError, AttributeError)`，捕获后返回 `{tool_name, query, hits: [], error: str(e)}`。
2. **SerpAPI 应用层错误**（无效 key、超额度等）：SerpAPI 在这类情况下返回 **HTTP 200**，但 JSON body 里带 `"error"` 字段（例如 `{"error": "Invalid API key."}`），不会触发 `raise_for_status()`。必须显式检查：拿到 `data = resp.json()` 后，若 `"error" in data`，直接把 `data["error"]` 作为错误信息返回，不再尝试解析 `organic_results`（否则会因为缺 key 抛出无意义的 `KeyError: 'organic_results'`，虽然也会被兜底捕获，但错误信息不可读）。

### 配置改动

`config.py`：

```python
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
```

替换原来的 `TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")`。`WEB_SEARCH_TIMEOUT`、`MOCK_WEB_SEARCH`、`MOCK_WEB_RESULTS_PATH` 保持不变（本来就是 provider 无关的命名）。

`tools/web_search.py` 顶部 docstring 里"调用 Tavily API"改成"调用 SerpAPI（Google 引擎）"。

## 测试

`tests/test_web_search.py`：

- `test_web_search_real_mode_parses_successful_response`：改成 mock `requests.get`（而不是 `requests.post`），响应体换成 SerpAPI 的 `organic_results` 格式，校验解析出的 `hits[0]` 字段映射正确。
- `test_web_search_real_mode_malformed_response_returns_error_dict`：同样改成 SerpAPI 格式，验证缺字段时仍走兜底路径返回 `error`。
- `test_web_search_real_mode_network_error_returns_error_dict`：改成 mock `requests.get` 抛超时，逻辑不变。
- 新增 `test_web_search_real_mode_api_error_in_200_response_returns_error_dict`：mock 一个 HTTP 200、body 为 `{"error": "Invalid API key."}` 的响应，验证 `web_search()` 返回 `hits: []` 且 `error` 字段等于 SerpAPI 给的原始错误信息（不是 `KeyError` 的字符串）。
- MOCK 模式相关的两个测试（`test_web_search_mock_mode_returns_structured_hits`、`test_web_search_mock_mode_respects_top_k`）不用改。

## 影响范围确认

- `agent/orchestrator.py` 和 `regulatory_policy_change` 假设逻辑通过 `web_search()` 的返回值消费结果，不直接依赖 Tavily 的字段名，不需要改动。
- 已核实 `README.md` / `BACKEND_INTEGRATION.md` 均未提及 `TAVILY_API_KEY`，无需同步改动。
