# 自定义异常输入 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `app.py` 加一个「自定义异常输入」tab，用户输入异常描述/指标名/基线期与异常期的时间范围和数值，驱动 `Orchestrator.run()` 排查；没有真实数据仓库支撑的看板类证据要显式降级为"数据源未接入"，不能用本地 mock CSV 冒充。

**Architecture:** `app.py` 拆成两个 `st.tabs`：「固定演示」（现有默认故事线，行为完全不变）和「自定义异常输入」（新增表单）。两个 tab 共享抽出来的 `render_trace()` / `render_conclusion()` 渲染函数。`data/scenario_runtime.py::use_scenario_data()` 放宽为接受 `None`，让 `tools/dashboard.py::query_dashboard()` 在没有数据源时返回明确的 `error` 字段——`agent/scoring.py` 已有的"遇到 `error` 就跳过该规则"逻辑天然处理降级，不用改任何 agent 内部代码。

**Tech Stack:** Python 3.12, Streamlit 1.37, pandas, pytest。`MOCK_LLM=1`（默认）跑测试，不依赖真实 API key。

## Global Constraints

- 所有面向用户的界面文案用中文，跟现有 `app.py` 风格一致。
- 不修改 `agent/orchestrator.py`、`agent/hypotheses.py`、`agent/scoring.py`、`agent/investigator.py`、`data/scenarios.py`（spec 明确的非目标）。
- 不接入真实数据仓库（Impala 等）；`tools/dashboard.py` 新增的降级分支要用注释清楚标出"生产环境接入点"，方便以后直接替换。
- 测试全部在 `MOCK_LLM=1`（默认值）下跑，不依赖网络或真实 API key。
- 每个任务完成后单独提交一次 commit，跟仓库现有的小步提交习惯一致。

---

## Task 1: `use_scenario_data()` 支持 `None`（禁用 dashboard 数据源）

**Files:**
- Modify: `data/scenario_runtime.py`
- Test: `tests/test_scenario_runtime.py`

**Interfaces:**
- Consumes: `config.DATA_PATH`（字符串或 `None`）、`tools.dashboard._load`（`functools.lru_cache` 装饰的函数，需要 `.cache_clear()`）
- Produces: `use_scenario_data(df: pd.DataFrame | None)` — context manager。`df` 为 `DataFrame` 时行为不变（写临时 CSV）；`df` 为 `None` 时把 `config.DATA_PATH` 设为 `None`，退出时恢复原值、清空 dashboard 缓存。后续任务（Task 4）依赖这个 `None` 分支。

- [ ] **Step 1: 写一个会失败的测试**

在 `tests/test_scenario_runtime.py` 末尾追加：

```python
def test_use_scenario_data_none_disables_dashboard(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with use_scenario_data(None):
        assert config.DATA_PATH is None

    assert config.DATA_PATH == str(original_csv)
    dashboard._load.cache_clear()
    result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
    assert result["records"][0]["value"] == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest tests/test_scenario_runtime.py::test_use_scenario_data_none_disables_dashboard -v`

Expected: FAIL——当前 `use_scenario_data()` 对 `df` 无条件调用 `df.to_csv(...)`，传 `None` 进去会抛 `AttributeError: 'NoneType' object has no attribute 'to_csv'`。

- [ ] **Step 3: 实现**

把 `data/scenario_runtime.py` 整个文件内容替换成：

```python
"""场景数据注入：让 tools/dashboard.py 在一个 with 块内读到指定的 DataFrame，
而不是默认的 config.DATA_PATH。

tools/dashboard.py::_load() 用 @lru_cache(maxsize=1) 缓存全局唯一一份数据，
只换 config.DATA_PATH 不清缓存的话，第二次调用还是读旧数据；用 context manager
包一层，保证正常退出和异常退出时都会恢复原路径、清空缓存，不污染同一进程内
后续对默认数据的读取。

df 为 None 时不写任何文件，直接把 config.DATA_PATH 设为 None——
query_dashboard() 遇到 None 会直接返回"数据源未接入"，不会尝试读取任何 CSV
（包括默认的 data/mock_loans.csv），用于「自定义异常输入」这种没有真实明细
数据的场景，避免用本地 mock 数据冒充成看板查询结果。
"""
from contextlib import contextmanager
import os
import shutil
import tempfile

import pandas as pd

import config
import tools.dashboard as dashboard


@contextmanager
def use_scenario_data(df: pd.DataFrame | None):
    original_path = config.DATA_PATH
    tmp_dir = None
    try:
        if df is None:
            config.DATA_PATH = None
        else:
            tmp_dir = tempfile.mkdtemp(prefix="scenario_data_")
            tmp_csv = os.path.join(tmp_dir, "data.csv")
            df.to_csv(tmp_csv, index=False)
            config.DATA_PATH = tmp_csv
        dashboard._load.cache_clear()
        yield
    finally:
        config.DATA_PATH = original_path
        dashboard._load.cache_clear()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest tests/test_scenario_runtime.py -v`

Expected: 全部 PASS（新测试 + 原有两个测试都要过，确认没有破坏 `df` 为真实 DataFrame 时的原有行为）。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenario_runtime.py tests/test_scenario_runtime.py
git commit -m "feat: use_scenario_data(None) disables dashboard data source"
```

---

## Task 2: `query_dashboard()` 在无数据源时优雅降级

**Files:**
- Modify: `tools/dashboard.py`
- Test: Create `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `config.DATA_PATH`（Task 1 产出的 `None` 状态）
- Produces: `query_dashboard(...)` 在 `config.DATA_PATH is None` 时返回 `{"tool_name": "query_dashboard", "error": "..."}`，不含 `records`/`summary` 字段，不抛异常。`agent/scoring.py::apply_rules()` 已有逻辑（看到 `error` 字段就跳过该规则）会消费这个返回值，本任务不需要改 `agent/scoring.py`。

- [ ] **Step 1: 写一个会失败的测试**

Create `tests/test_dashboard.py`:

```python
"""query_dashboard() 在 config.DATA_PATH 未配置（None）时的降级行为——
供「自定义异常输入」模式使用，避免在没有真实明细数据时误读任何本地 CSV。"""
import config
import tools.dashboard as dashboard


def test_query_dashboard_returns_error_when_data_path_is_none(monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", None)
    dashboard._load.cache_clear()

    result = dashboard.query_dashboard("fpd30_rate", "2026-06-01~2026-07-30")

    assert result["tool_name"] == "query_dashboard"
    assert "error" in result
    assert "records" not in result
    assert "summary" not in result
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest tests/test_dashboard.py -v`

Expected: FAIL——当前 `_load()` 会对 `config.DATA_PATH=None` 调用 `pd.read_csv(None)`，抛 `TypeError` 或类似异常，不会返回 `error` 字典。

- [ ] **Step 3: 实现**

在 `tools/dashboard.py` 里，`def query_dashboard(...)` 函数体最开头（`df = _load()` 之前）插入：

```python
def query_dashboard(metric_name: str, date_range: str, dimensions: list | None = None) -> dict:
    """date_range 格式: 'YYYY-MM-DD~YYYY-MM-DD'。dimensions 为空则返回时间序列。"""
    # ── 生产环境接入点 ──
    # 真实环境里 config.DATA_PATH 应该指向公司数据仓库的查询封装（如 Impala），
    # schema 保持不变，下面基于 pandas 的实现直接整段替换掉即可，
    # 上层 agent 逻辑（scoring/investigator/orchestrator）不用动。
    # 接入后可以删掉这个 if 分支——它只是"还没接入真实数据源"时的降级。
    if config.DATA_PATH is None:
        return {"tool_name": "query_dashboard",
                "error": "数据源未接入：生产环境将对接公司数据仓库（如 Impala），当前演示模式下不可用"}

    df = _load()
    start, end = date_range.split("~")
    ...  # 其余逻辑不变
```

（`...` 后面是文件里已有的代码，原样保留，不要删除或改动。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest tests/test_dashboard.py tests/test_scenario_runtime.py tests/test_smoke.py -v`

Expected: 全部 PASS。`test_smoke.py` 里 `test_orchestrator_runs_end_to_end()` 用的是默认 `config.DATA_PATH`（不是 `None`），走的还是原有的读 CSV 逻辑，确认没有回归。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add tools/dashboard.py tests/test_dashboard.py
git commit -m "feat: query_dashboard degrades gracefully when no data source configured"
```

---

## Task 3: `app.py` 拆成两个 tab，抽出共享渲染函数（固定演示行为不变）

**Files:**
- Modify: `app.py`（整体重写，逻辑对「固定演示」部分保持等价）

**Interfaces:**
- Consumes: `agent.orchestrator.Orchestrator`、`agent.hypotheses.HYP_LABELS`、`tools.dashboard.query_dashboard`（均为已有接口，不变）
- Produces: 模块级函数 `render_trace(trace: list[dict]) -> None`、`render_conclusion(final: dict) -> None`，供 Task 4 的自定义 tab 复用。`tab_fixed`, `tab_custom = st.tabs([...])` 两个 tab 对象，Task 4 会在 `tab_custom` 里继续添加内容。

**背景（写这个任务前必读）：** 现在 `app.py` 只有一条路径——固定读 `data/mock_loans.csv`，`Orchestrator().run()` 不传任何参数。这个任务只是把这条路径原封不动地包进 `tab_fixed`，同时把"渲染 trace 步骤"和"渲染最终结论"这两段逻辑抽成两个函数，方便下一个任务里新的 tab 复用。这个任务完成后，「固定演示」tab 的界面表现应该跟改动前的 `app.py` 一模一样。

- [ ] **Step 1: 用下面的完整内容覆盖 `app.py`**

```python
"""Streamlit 前端：展示排查全过程（推理过程可视化是核心卖点）。

运行: streamlit run app.py
"""
from datetime import date

import pandas as pd
import streamlit as st

import config
from tools.dashboard import query_dashboard
from agent.orchestrator import Orchestrator
from agent.hypotheses import HYP_LABELS
from data.scenario_runtime import use_scenario_data

st.set_page_config(page_title="信贷数据异常排查 Agent", layout="wide")
st.title("信贷数据异常排查 Agent")
st.caption(f"模式: {'MOCK（无需API key）' if config.MOCK_LLM else f'Kimi {config.LLM_MODEL}'} ｜ "
           f"查询预算: {config.MAX_TOOL_CALLS} 次 ｜ 收敛阈值: 领先 {config.CONVERGE_MARGIN} 分")


# ── 共享渲染逻辑：两个 tab 都用同一份 ────────────────────
def render_trace(trace: list[dict]) -> None:
    for step in trace:
        if step["step_type"] == "think":
            st.info(f"🧠 **Think** (round {step['round']})　{step['text']}")
        elif step["step_type"] == "reflect":
            st.warning(f"🔍 **Reflect 自我核验**　{step['text']}")
        else:
            tag = "🔁 [核验补查] " if step.get("is_reflect") else ""
            with st.expander(
                f"🛠 {tag}{HYP_LABELS.get(step['hypothesis'], step['hypothesis'])} ｜ {step['question']} "
                f"→ `{step['tool_name']}`" + ("　*(已折叠)*" if step.get("folded") else "")
            ):
                st.code(str(step["tool_args"]), language="python")
                st.write("**摘要**:", step["digest"])
                if not step.get("folded") and "raw_result" in step:
                    st.json(step["raw_result"], expanded=False)
                for ev in step["evidence_fired"]:
                    sign = "＋" if ev["contribution"] >= 0 else "－"
                    st.success(
                        f"{sign} 证据触发 [{ev['rule_id']}] {ev['desc']}\n\n"
                        f"　详情: {ev['detail']}\n\n"
                        f"　贡献 = 权重 {ev['weight']} × 置信度 {ev['confidence']:.2f} "
                        f"= **{ev['contribution']:+.3f}** → {HYP_LABELS.get(ev['hypothesis'], ev['hypothesis'])}"
                        + (f"　*(LLM判断)*" if ev["judge_type"] == "llm_judgment" else "　*(规则判断)*")
                    )


def render_conclusion(final: dict) -> None:
    st.header("③ 归因结论")
    rank_df = pd.DataFrame([
        {"假设": HYP_LABELS.get(h["hyp_id"], h["hyp_id"]), "置信度": h["score"]} for h in final["ranking"]
    ]).set_index("假设")
    st.bar_chart(rank_df, height=220, horizontal=True)

    st.markdown(
        f"### 最终归因：**{HYP_LABELS.get(final['conclusion_id'], final['conclusion_id'])}**（置信度 {final['confidence']:.2f}）\n"
        f"- 收敛状态: {'✅ 提前收敛' if final['converged'] else '⏱ 预算耗尽后取最高分'}\n"
        f"- 查询预算使用: {final['tool_calls_used']} / {final['budget']}\n"
        + (f"- 次优假设: {final['runner_up']['desc']}（{final['runner_up']['score']:.2f}）"
           if final["runner_up"] else "")
    )

    with st.expander("完整证据链（evidence_log）"):
        for h in final["ranking"]:
            if h["evidence"]:
                st.markdown(f"**{HYP_LABELS.get(h['hyp_id'], h['hyp_id'])}** — 总分 {h['score']:.3f}")
                st.table(pd.DataFrame(h["evidence"])[
                    ["rule_id", "desc", "detail", "weight", "confidence", "contribution"]])


tab_fixed, tab_custom = st.tabs(["固定演示", "自定义异常输入"])

# ── Tab 1: 固定演示（行为与改动前完全一致） ───────────────
with tab_fixed:
    st.header("① 异常现象")
    overview = query_dashboard("fpd30_rate", "2026-06-01~2026-07-30")
    df = pd.DataFrame(overview["records"])
    base = df[df["date"] < "2026-07-20"]["value"].mean()
    recent = df[df["date"] >= "2026-07-20"]["value"].mean()

    c1, c2, c3 = st.columns(3)
    c1.metric("基线期 FPD30", f"{base:.2%}")
    c2.metric("异常期 FPD30", f"{recent:.2%}", delta=f"{(recent/base-1):+.0%}", delta_color="inverse")
    c3.metric("异常期起点", "2026-07-20")
    st.line_chart(df.set_index("date")["value"], height=220)

    st.header("② Agent 排查过程")
    if st.button("开始排查", type="primary", key="fixed_run"):
        with st.spinner("Agent 排查中…"):
            orch = Orchestrator()
            final = orch.run()
        st.session_state["trace"] = orch.trace
        st.session_state["final"] = final

    if "trace" in st.session_state:
        render_trace(st.session_state["trace"])
        render_conclusion(st.session_state["final"])

# ── Tab 2: 自定义异常输入（Task 4 补齐表单和排查逻辑） ─────
with tab_custom:
    st.info(
        "演示模式未接入公司数据仓库，看板类证据（渠道占比 / 分渠道逾期率 / "
        "平均信用分 / 放款笔数）暂不可用，归因结论目前只基于知识库检索和网络检索——"
        "接入真实数据源后可补齐。"
    )
```

- [ ] **Step 2: 语法检查**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m py_compile app.py`

Expected: 无输出、无报错（退出码 0）。

- [ ] **Step 3: 手动验证「固定演示」tab 行为不变**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && MOCK_LLM=1 streamlit run app.py`

在浏览器打开的页面里：
1. 确认顶部标题、caption、页面停留在「固定演示」tab 时能看到"① 异常现象"三个指标卡片和折线图（跟改动前一致）。
2. 点「开始排查」，确认能看到 Think/Reflect/工具调用 展开项和最终"③ 归因结论"部分正常渲染，跟改动前观感一致。
3. 切到「自定义异常输入」tab，确认能看到顶部的蓝色提示条（"演示模式未接入公司数据仓库…"），此时还没有表单（Task 4 才加）。

停止 streamlit 进程（Ctrl+C）。

- [ ] **Step 4: 跑全量测试确认没有回归**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest -q`

Expected: 全部 PASS（`app.py` 本身没有自动化测试覆盖，这一步是确认这次改动没有牵连到其他模块）。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add app.py
git commit -m "refactor: split app.py into tabs, extract shared render_trace/render_conclusion"
```

---

## Task 4: 「自定义异常输入」tab 表单 + 排查逻辑

**Files:**
- Modify: `app.py`

**Interfaces:**
- Consumes: Task 1 的 `use_scenario_data(None)`、Task 3 的 `render_trace()` / `render_conclusion()` / `tab_custom`、`agent.orchestrator.Orchestrator.run(phenomenon, metric_name, baseline_range, anomaly_range, direction)`（已有签名，不改）
- Produces: 无新的模块级接口，纯 UI 逻辑；`st.session_state["custom_trace"]` / `st.session_state["custom_final"]`（与固定演示 tab 的 `"trace"`/`"final"` 分开存放，互不覆盖）

- [ ] **Step 1: 在 `tab_custom` 里补齐表单和排查逻辑**

用 Edit 把 `app.py` 里 Task 3 留下的这一段：

```python
with tab_custom:
    st.info(
        "演示模式未接入公司数据仓库，看板类证据（渠道占比 / 分渠道逾期率 / "
        "平均信用分 / 放款笔数）暂不可用，归因结论目前只基于知识库检索和网络检索——"
        "接入真实数据源后可补齐。"
    )
```

改成：

```python
def _fmt_date_range(date_pair: tuple) -> str:
    start, end = date_pair
    return f"{start.isoformat()}~{end.isoformat()}"


with tab_custom:
    st.info(
        "演示模式未接入公司数据仓库，看板类证据（渠道占比 / 分渠道逾期率 / "
        "平均信用分 / 放款笔数）暂不可用，归因结论目前只基于知识库检索和网络检索——"
        "接入真实数据源后可补齐。"
    )

    with st.form("custom_anomaly_form"):
        phenomenon = st.text_area("异常描述", placeholder="如：新增用户FPD30逾期率近期环比明显上升")
        metric_name = st.text_input("指标名称", value="fpd30_rate")
        baseline_range = st.date_input("基线期", value=(date(2026, 6, 1), date(2026, 7, 19)))
        anomaly_range = st.date_input("异常期", value=(date(2026, 7, 20), date(2026, 7, 30)))
        baseline_value = st.number_input("基线期水平", format="%.4f", min_value=0.0001, value=0.03)
        anomaly_value = st.number_input("异常期水平", format="%.4f", min_value=0.0, value=0.05)
        submitted = st.form_submit_button("开始排查", type="primary")

    if submitted:
        if not phenomenon.strip():
            st.error("请填写异常描述")
        elif len(baseline_range) != 2 or len(anomaly_range) != 2:
            st.error("请选择完整的日期范围（起止两个日期）")
        else:
            c1, c2 = st.columns(2)
            c1.metric("基线期水平", f"{baseline_value:.2%}")
            c2.metric("异常期水平", f"{anomaly_value:.2%}",
                       delta=f"{(anomaly_value/baseline_value-1):+.0%}", delta_color="inverse")
            direction = "UP" if anomaly_value > baseline_value else "DOWN"
            with st.spinner("Agent 排查中…"):
                with use_scenario_data(None):
                    orch = Orchestrator()
                    final = orch.run(
                        phenomenon=phenomenon,
                        metric_name=metric_name,
                        baseline_range=_fmt_date_range(baseline_range),
                        anomaly_range=_fmt_date_range(anomaly_range),
                        direction=direction,
                    )
            st.session_state["custom_trace"] = orch.trace
            st.session_state["custom_final"] = final

    if "custom_trace" in st.session_state:
        st.header("② Agent 排查过程")
        render_trace(st.session_state["custom_trace"])
        render_conclusion(st.session_state["custom_final"])
```

- [ ] **Step 2: 语法检查**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m py_compile app.py`

Expected: 无输出、无报错。

- [ ] **Step 3: 手动验证自定义模式端到端**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && MOCK_LLM=1 streamlit run app.py`

在「自定义异常输入」tab：
1. 异常描述填"新增用户FPD30逾期率近期环比明显上升，怀疑某个渠道放量导致"，指标名称保持默认 `fpd30_rate`，基线期/异常期用默认日期，基线期水平填 `0.032`，异常期水平填 `0.048`，点「开始排查」。
2. 确认顶部出现基线/异常两个指标卡片，数值和 delta 符号正确（异常期 > 基线期，应显示上升）。
3. 展开证据链，确认涉及 `query_dashboard` 的探测步骤（如"各渠道放款占比近期是否发生结构性变化"）摘要里包含"数据源未接入"字样；确认涉及 `retrieve_historical_case`/`retrieve_business_kb`/`web_search` 的探测步骤正常返回内容，不受影响。
4. 确认"③ 归因结论"正常渲染出结论、置信度、排名柱状图（置信度可能偏低甚至是 `other_unclassified`，这是预期行为，不是 bug）。
5. 切回「固定演示」tab，点「开始排查」，确认这条路径完全不受自定义 tab 刚才运行的影响（结论跟 Task 3 验证时一致）。

停止 streamlit 进程（Ctrl+C）。

- [ ] **Step 4: 跑全量测试确认没有回归**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest -q`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add app.py
git commit -m "feat: add custom anomaly input form driving Orchestrator.run() directly"
```

---

## Task 5: 最终整体回归

**Files:** 无新增/修改文件，纯验证。

- [ ] **Step 1: 跑全量测试**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && python -m pytest -q`

Expected: 全部 PASS（预期总数 = 改动前 67 个 + Task 1 新增 1 个 + Task 2 新增 1 个 = 69 个）。

- [ ] **Step 2: 确认 git log 里四个任务的提交都在**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && git log --oneline -5`

Expected: 看到 Task 1~4 的四条 commit（`feat: use_scenario_data(None)...` / `feat: query_dashboard 降级...` / `refactor: split app.py...` / `feat: add custom anomaly input form...`）。

- [ ] **Step 3: 更新项目记忆**

跟用户确认这轮（子项目 C）已完成，后续按优先级顺序进入 A（`tools/web_search.py` 换成 Google 免费方案）。这一步不改代码，只是收尾确认，供后续会话参考。
