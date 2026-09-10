# 自定义异常输入 UI 设计（子项目 C）

## 背景

`app.py` 目前只有一条路径：用户点击「开始排查」，`Orchestrator().run()` 用全部默认参数跑固定的默认故事线（读 `data/mock_loans.csv`）。没有任何输入或选择，无法演示"用户描述一个真实异常，Agent 据此排查"这个更贴近实际使用场景的能力。

这个仓库最终目标是接入公司真实数据源（Java 后端 + 数据仓库，可能是 Impala 之类），但当前只能在个人电脑上开发、还没确定后续用什么方式连接真实数据。因此这次要做的不是"帮用户拼一份新的模拟数据再排查"（那只是换了个数据模拟器，没有实际意义），而是让用户直接输入异常的**指标名称、描述文字、基线水平、异常期水平**，Agent 基于这些信息排查；排查过程中原本依赖看板明细数据下钻的证据（渠道占比、分渠道逾期率、平均信用分、放款笔数）在没有接入真实数据源时应该显式降级、不可用，而不是假装用本地 mock CSV 蒙混过去。

关键发现：`agent/orchestrator.py::Orchestrator.run()` 已经接受 `phenomenon` / `metric_name` / `baseline_range` / `anomaly_range` / `direction` / `anomaly_type` 等参数（应为之前 Java 后端对接预留），不需要改动 Orchestrator 本身的签名，只需要在 UI 层把表单值传进去。

## 目标

1. `app.py` 增加两个 tab：「固定演示」（现有行为不变）和「自定义异常输入」（新增，本次范围）。
2. 自定义异常输入 tab：表单收集 指标名称 / 异常描述 / 基线期时间范围 / 异常期时间范围 / 基线期水平数值 / 异常期水平数值，点击「开始排查」后用这些值调用 `Orchestrator.run()`，复用现有的证据链 + 结论渲染 UI（`app.py` 现有的第②③屏渲染代码，本次不改）。
3. 在自定义模式下，`query_dashboard` 优雅降级为"数据源未接入"，不读取任何本地 mock CSV；证据引擎已有的"`error` 字段规则不触发"逻辑天然处理这个降级，不需要改 `agent/scoring.py`。
4. 降级分支的代码要清楚标注"以后接入真实数据仓库（如 Impala）时如何替换"，方便直接换掉，而不是留一段孤立的、意图不明的判断。
5. UI 上用一条常驻提示告知用户当前处于未接入真实数据源的降级状态，说明会影响哪些证据、进而影响最终置信度。

## 非目标

- 不做场景选择器（`data/scenarios.py` 里的 6 个评估场景不接入 `app.py`，那是 `scripts/eval_scenarios.py` 的专用回归测试数据，跟这次"用户描述真实异常"的方向无关）。
- 不做异常幅度/严重度滑块之类的数据生成参数化——那本质上还是在做数据模拟器，不是这次要做的事。
- 不接入真实数据仓库（Impala 等）本身——这次只把"未接入时如何优雅降级 + 预留清晰的替换点"做好，真正的连接逻辑留到确定了连接方式之后再做。
- 不在 UI 暴露 `suspect_segments`（Java 后端可以预先定位可疑细分、跳过部分探测步骤的机制）——这是给 Java 后端调用准备的参数，人工填表单场景不适用。
- 不给 `direction` 做单独输入控件——由用户填的基线/异常数值自动推断（异常期数值 > 基线期数值则为 `UP`，否则 `DOWN`）。
- 不改 `agent/hypotheses.py` 的假设池结构、`agent/scoring.py` 的规则引擎、`agent/orchestrator.py` 的主循环逻辑。

## 架构

```
data/scenario_runtime.py   改：use_scenario_data(df) 的类型放宽为 df: pd.DataFrame | None，
                                 传 None 时不写临时 CSV，直接把 config.DATA_PATH 设为 None
                                 （复用同一套 try/finally 恢复 + 清缓存逻辑，不新增函数）
tools/dashboard.py         改：query_dashboard() 开头加判断——
                                 config.DATA_PATH is None 时直接返回
                                 {"tool_name": "query_dashboard", "error": "..."}，
                                 不尝试 pd.read_csv()
app.py                     改：加 st.tabs（固定演示 / 自定义异常输入）；
                                 现有第①②③屏代码原样挪进「固定演示」tab；
                                 「自定义异常输入」tab 新增表单 + 顶部指标对比卡片
                                 + 复用第②③屏的证据链/结论渲染逻辑（抽成局部函数共享）
```

不改动 `agent/orchestrator.py`、`agent/hypotheses.py`、`agent/scoring.py`、`agent/investigator.py`、`data/scenarios.py`。

## 组件详细设计

### 1. `data/scenario_runtime.py` 改动

```python
@contextmanager
def use_scenario_data(df: pd.DataFrame | None):
    """df 为 DataFrame 时：写成临时 CSV，让 query_dashboard() 在 with 块内读到这份数据。
    df 为 None 时：不写任何文件，直接把 config.DATA_PATH 设为 None——
    query_dashboard() 遇到 None 会直接返回"数据源未接入"，不会尝试读取任何 CSV
    （包括默认的 data/mock_loans.csv），用于「自定义异常输入」这种没有真实明细数据
    的场景，避免用本地 mock 数据冒充成看板查询结果。
    两种情况退出时（正常返回或抛异常）都会恢复原 config.DATA_PATH 并清空
    dashboard 的 lru_cache，不污染同一进程内后续的数据读取。"""
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

### 2. `tools/dashboard.py` 改动

```python
def query_dashboard(metric_name: str, date_range: str, dimensions: list | None = None) -> dict:
    """date_range 格式: 'YYYY-MM-DD~YYYY-MM-DD'。dimensions 为空则返回时间序列。"""
    # ── 生产环境接入点 ──
    # 真实环境里 config.DATA_PATH 应该指向公司数据仓库的查询封装
    # （如 Impala），schema 保持不变，下面的 pandas 实现直接整段替换掉即可，
    # 上层 agent 逻辑（scoring/investigator/orchestrator）不用动。
    # 接入后可以删掉这个 if 分支——它只是"还没接入真实数据源"时的降级。
    if config.DATA_PATH is None:
        return {"tool_name": "query_dashboard",
                "error": "数据源未接入：生产环境将对接公司数据仓库（如 Impala），当前演示模式下不可用"}

    df = _load()
    ...  # 其余逻辑不变
```

`agent/scoring.py::apply_rules()` 已有的"`tool_result` 含 `error` 字段则跳过该规则、不触发不报错"逻辑（原本是为 `web_search` 超时准备的）天然覆盖这个新的降级路径，不需要改动。

### 3. `app.py` 改动

结构调整为：

```python
tab_fixed, tab_custom = st.tabs(["固定演示", "自定义异常输入"])

with tab_fixed:
    # 现有①②③屏代码原样搬进来，不改逻辑

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
        baseline_value = st.number_input("基线期水平", format="%.4f", min_value=0.0001)
        anomaly_value = st.number_input("异常期水平", format="%.4f", min_value=0.0)
        submitted = st.form_submit_button("开始排查", type="primary")

    if submitted:
        c1, c2 = st.columns(2)
        c1.metric("基线期水平", f"{baseline_value:.2%}")
        c2.metric("异常期水平", f"{anomaly_value:.2%}",
                   delta=f"{(anomaly_value/baseline_value-1):+.0%}", delta_color="inverse")
        direction = "UP" if anomaly_value > baseline_value else "DOWN"
        with st.spinner("Agent 排查中…"):
            with use_scenario_data(None):
                orch = Orchestrator()
                final = orch.run(
                    phenomenon=phenomenon, metric_name=metric_name,
                    baseline_range=_fmt_range(baseline_range),
                    anomaly_range=_fmt_range(anomaly_range),
                    direction=direction,
                )
        st.session_state["custom_trace"] = orch.trace
        st.session_state["custom_final"] = final

    if "custom_trace" in st.session_state:
        render_trace(st.session_state["custom_trace"])
        render_conclusion(st.session_state["custom_final"])
```

现有第②③屏里"渲染 trace 步骤"（原第 39~63 行）和"渲染结论"（原第 65~86 行）这两段目前直接写在模块顶层、读全局 `st.session_state["trace"]`/`["final"]`，改成两个函数 `render_trace(trace)` / `render_conclusion(final)`，接参数而不是读固定 key 的 session_state，「固定演示」tab 和「自定义异常输入」tab 分别传各自的 session_state key（`trace`/`final` vs `custom_trace`/`custom_final`）调用，避免两个 tab 之间状态互相覆盖。这是本次唯一的"顺手重构"，直接服务于两个 tab 复用同一套渲染逻辑这个需求，不额外扩大范围。

`_fmt_range(date_tuple)` 是一个小helper，把 `st.date_input` 返回的 `(start_date, end_date)` 格式化成 `Orchestrator.run()` 要求的 `"YYYY-MM-DD~YYYY-MM-DD"` 字符串。

## 数据流（自定义异常输入模式）

```
用户填表单提交
  → direction 由 anomaly_value vs baseline_value 自动推断
  → with use_scenario_data(None):
        config.DATA_PATH 被设为 None，dashboard 缓存清空
        Orchestrator.run(phenomenon, metric_name, baseline_range, anomaly_range, direction)
          → build_hypothesis_tree(metric_name, baseline_range, anomaly_range) 正常构建 5 个假设的 probe
          → probe 执行到 query_dashboard 时：
                config.DATA_PATH is None → 返回 {"error": "数据源未接入..."}
                apply_rules() 看到 error 字段 → 该 probe 绑定的规则全部跳过、不触发
          → probe 执行到 retrieve_historical_case / retrieve_business_kb / web_search 时：
                不受影响，正常查询 KB（kb/business_kb.md、kb/historical_cases.json）和 web_search
          → 汇总打分、置信度地板判断 → final
  → with 块退出：config.DATA_PATH 恢复原值，缓存清空，不影响「固定演示」tab 后续使用
→ render_trace(final 对应的 trace) / render_conclusion(final) 渲染
```

## 错误处理

- `use_scenario_data(None)` 的 `finally` 块保证即使 `Orchestrator().run()` 内部抛异常，`config.DATA_PATH` 也会恢复原值、缓存会被清空，不会让「固定演示」tab 后续运行读到 `None` 路径而崩溃。
- `query_dashboard()` 在 `config.DATA_PATH is None` 时直接返回 `error` 字典，不会抛 `FileNotFoundError`（不传 `None` 进 `pd.read_csv`）。
- 表单未填写必填字段（异常描述为空等）时用 `st.form_submit_button` 原生校验 + 简单的空值检查阻止提交，不调用 `Orchestrator.run()`。
- `baseline_value` 为 0 时 `anomaly_value/baseline_value-1` 会除零；表单里给 `baseline_value` 的 `st.number_input` 设 `min_value=0.0001`，从控件层面禁止提交 0，不需要额外的运行时判空分支。

## 测试

- `tests/test_scenario_runtime.py`：新增用例，断言 `use_scenario_data(None)` 退出后 `config.DATA_PATH` 恢复为原值、`dashboard._load` 缓存被清空；`with` 块内 `config.DATA_PATH is None` 成立。
- `tests/test_dashboard.py`（如不存在则新建）：断言 `config.DATA_PATH is None` 时 `query_dashboard(...)` 返回的 dict 含 `error` 字段、不含 `records`/`summary`，且不抛异常。
- 回归确认：`use_scenario_data(df)`（`df` 为真实 DataFrame，非 `None`）的原有行为不受影响，`tests/test_scenario_runtime.py` 里已有的旧测试和 `tests/test_multi_factor_scenario.py`、`scripts/eval_scenarios.py` 继续全部通过。
- 手动验证：`streamlit run app.py`，「自定义异常输入」tab 填一份表单跑一次，确认——① 顶部提示可见；② 证据链里 dashboard 相关的 probe 摘要显示"数据源未接入"；③ KB/web_search 相关的 probe 正常给出结果；④ 结论渲染正常（含置信度可能偏低甚至落入 `other_unclassified` 的情况）；⑤ 切回「固定演示」tab 再跑一次，确认不受自定义 tab 运行过的影响（数据源已正确恢复）。

## 已知限制

- 自定义模式下，`channel_quality_decline`（依赖 r1/r2，权重占比 0.60/0.80）、`approval_policy_change`（依赖 r4，权重占比 0.35/0.65 且另一条 r5 是反证）、`data_quality_issue`（依赖 r7，权重占比 0.40/0.55）这三个假设的看板类 probe 会因为 `query_dashboard` 返回 `error` 而不触发任何规则，分数明显偏低。但这不代表 `macro_seasonality`（0.35，纯 KB）和 `regulatory_policy_change`（0.60，KB+web_search）就不受影响：`agent/orchestrator.py::_pick_hypothesis()` 按当前分数（并列时按剩余 probe 数）best-first 选择要展开的假设，看板依赖型的 probe 即使从不触发证据，执行一次仍然会照样消耗 `config.MAX_TOOL_CALLS`（当前 8 次）里的一次预算。也就是说，如果看板依赖型 probe 先被选中展开、把预算耗光，`regulatory_policy_change` 的 probe（`p_reg_search`/`p_reg_kb`——本该是唯一能仅凭 web_search 就得出置信结论的假设）可能根本轮不到被展开，而不仅仅是"分数不受影响"。实测中确实出现过预算耗尽在看板类 probe 上、`regulatory_policy_change` 从未获得展开机会的情况。因此自定义模式下大概率会得到 `other_unclassified`，是否偏向宏观/监管类结论取决于 best-first 选择顺序和预算是否耗尽在看板 probe 上，并不保证。这是当前证据结构 + best-first 调度决定的真实限制，不是 bug，会在 UI 提示里说明，不做人为调权重或改动 orchestrator 调度逻辑来掩盖。
- `baseline_range`/`anomaly_range` 在自定义模式下只影响 probe 的展示文案和 web_search 的查询词拼接，不会被真正用来筛选任何明细数据（因为没有明细数据可筛）。

## 不在本次范围内（后续可能的方向）

- 真正接入公司数据仓库（Impala 等），替换 `tools/dashboard.py` 里标注的"生产环境接入点"分支。
- 针对自定义模式单独校准置信度地板或证据权重，缓解"看板证据缺失导致大概率 other_unclassified"的倾向。
- `suspect_segments` 先验注入在自定义模式表单里的暴露（目前仍只服务 Java 后端调用路径）。
