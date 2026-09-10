# web_search: Tavily → SerpAPI Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the real-mode HTTP call in `tools/web_search.py` from Tavily's API to SerpAPI (Google engine), keeping the function's external contract (signature, return shape) unchanged.

**Architecture:** `tools/web_search.py::web_search()` has two branches: `_mock_search()` (untouched) and a real-mode branch that currently POSTs to Tavily. That branch becomes a GET to `https://serpapi.com/search.json`, parsing SerpAPI's `organic_results` array instead of Tavily's `results` array. `config.py` gets a new `SERPAPI_API_KEY` setting replacing `TAVILY_API_KEY`.

**Tech Stack:** Python, `requests`, `pytest` + `pytest`'s `monkeypatch` fixture.

## Global Constraints

- Return shape of `web_search()` must stay exactly `{"tool_name": "web_search", "query": query, "hits": [...]}` on success, or `{"tool_name": "web_search", "query": query, "hits": [], "error": str}` on any failure — this is relied on by `agent/orchestrator.py` and covered by existing MOCK-mode tests, which must keep passing unmodified.
- Each hit dict must have exactly the keys `{title, url, snippet, published_date}`.
- Never let a network or parsing exception propagate out of `web_search()` — `Orchestrator.run()` is synchronous and a raised exception would hang the whole diagnosis run.
- No provider-abstraction layer, no `hl`/`gl` region params — out of scope per the approved spec (`docs/superpowers/specs/2026-08-15-web-search-serpapi-swap-design.md`).

---

## Task 1: Swap request/response handling to SerpAPI, update config

**Files:**
- Modify: `config.py:33` (rename `TAVILY_API_KEY` → `SERPAPI_API_KEY`)
- Modify: `tools/web_search.py` (docstring + real-mode branch of `web_search()`)
- Modify: `tests/test_web_search.py` (update the three real-mode tests that aren't the new 200-with-error-body case)

**Interfaces:**
- Consumes: nothing new — `config.WEB_SEARCH_TIMEOUT` (unchanged) still supplies the request timeout.
- Produces: `config.SERPAPI_API_KEY` (str, default `""`), used by `tools/web_search.py`. `web_search(query: str, top_k: int = 3) -> dict` signature and return shape unchanged — later tasks and existing callers rely on this.

- [ ] **Step 1: Update `config.py`**

Change line 33 from:
```python
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
```
to:
```python
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
```

- [ ] **Step 2: Update the three existing real-mode tests in `tests/test_web_search.py` to describe SerpAPI behavior (this is the "write failing test" step for this task)**

Replace `test_web_search_real_mode_parses_successful_response`:
```python
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
```

Replace `test_web_search_real_mode_network_error_returns_error_dict`:
```python
def test_web_search_real_mode_network_error_returns_error_dict(monkeypatch):
    monkeypatch.setattr(config, "MOCK_WEB_SEARCH", False)

    def fake_get(url, params, timeout):
        raise requests.exceptions.Timeout("connection timed out")

    monkeypatch.setattr(requests, "get", fake_get)

    result = web_search("测试查询", top_k=3)

    assert result["tool_name"] == "web_search"
    assert result["hits"] == []
    assert "error" in result
```

Replace `test_web_search_real_mode_malformed_response_returns_error_dict`:
```python
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
```

- [ ] **Step 3: Run the updated tests to verify they fail**

Run: `pytest tests/test_web_search.py -v`
Expected: The three tests touched in Step 2 FAIL (the two mock-mode tests still pass, since `_mock_search` isn't touched yet). Failures should show `AttributeError: module 'config' has no attribute 'SERPAPI_API_KEY'` or the code still POSTing to Tavily's URL — confirming the tests now describe SerpAPI behavior that doesn't exist yet.

- [ ] **Step 4: Implement the SerpAPI swap in `tools/web_search.py`**

Replace the file's docstring first line:
```python
"""工具4: web_search —— 外部监管/行业新闻检索（只读，调用 SerpAPI，Google 引擎）。
```

Replace the `try` block inside `web_search()` (lines 20-43) with:
```python
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
```

(The SerpAPI-200-with-error-body case is handled in Task 2 — don't add it here.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_search.py -v`
Expected: All 5 existing tests PASS (2 mock-mode tests unaffected, 3 real-mode tests now pass against the SerpAPI implementation).

- [ ] **Step 6: Commit**

```bash
git add config.py tools/web_search.py tests/test_web_search.py
git commit -m "feat: swap web_search real-mode provider from Tavily to SerpAPI"
```

---

## Task 2: Handle SerpAPI's HTTP-200-with-error-body case

**Files:**
- Modify: `tools/web_search.py` (real-mode branch of `web_search()`, built on top of Task 1)
- Test: `tests/test_web_search.py` (new test)

**Interfaces:**
- Consumes: `web_search(query: str, top_k: int = 3) -> dict` from Task 1 — same function, extending its real-mode branch.
- Produces: no new public interface; behavior addition only (an SerpAPI application-level error, delivered as HTTP 200 with `{"error": "..."}` body, must be surfaced as `result["error"] == "<SerpAPI's message>"` instead of failing on a `KeyError` for the missing `organic_results` key).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web_search.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_search.py::test_web_search_real_mode_api_error_in_200_response_returns_error_dict -v`
Expected: FAIL — without the explicit check, `data.get("organic_results", [])` returns `[]` (not a `KeyError`), so `hits == []` but `result["error"]` is absent (`assert result["error"] == "Invalid API key."` fails with `KeyError: 'error'`).

- [ ] **Step 3: Implement the explicit error-body check**

In `tools/web_search.py`, inside the `try` block from Task 1, insert a check right after `data = resp.json()` and before building `hits`:
```python
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return {"tool_name": "web_search", "query": query, "hits": [], "error": data["error"]}
        hits = [
```
(the rest of the `hits` list comprehension and the `except` clause stay exactly as Task 1 left them)

- [ ] **Step 4: Run the full test file to verify everything passes**

Run: `pytest tests/test_web_search.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/web_search.py tests/test_web_search.py
git commit -m "feat: surface SerpAPI application-level errors (HTTP 200 + error body)"
```

---

## Task 3: Full-suite regression check

**Files:** none modified — verification only.

**Interfaces:**
- Consumes: entire test suite as it stands after Tasks 1-2.
- Produces: nothing — this task is a gate before merging sub-project A back to `main`.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: All tests pass, including `tests/test_web_search.py` (6 tests) and any test elsewhere that touches `web_search` (e.g. `tests/test_orchestrator.py` if it exercises the `regulatory_policy_change` hypothesis) — confirm none of those reference `TAVILY_API_KEY` or Tavily-shaped mock data.

- [ ] **Step 2: Grep for any leftover Tavily references**

Run: `grep -rn "TAVILY\|tavily" --include="*.py" --include="*.md" .`
Expected: No output (the spec already confirmed `README.md`/`BACKEND_INTEGRATION.md` don't mention it; this step re-confirms nothing was missed in `.py` files or elsewhere).

- [ ] **Step 3: Report completion**

No commit — this task only verifies. If Step 1 or Step 2 finds anything, fix it as part of Task 1 or Task 2's scope (re-open that task, don't create a new one), then re-run this task.
