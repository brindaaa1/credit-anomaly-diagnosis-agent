# 硬编码清理 + 归因结论置信度兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 `scripts/eval_scenarios.py` 与未来复用场景之间的重复逻辑、修掉 `app.py` 里跟假设池不同步的硬编码标签字典、并给 `Orchestrator` 加一个置信度地板，使证据不足时诚实返回"未归类"而不是强行套进 5 个假设之一。

**Architecture:** 四段独立但相关的改动，按依赖顺序排列：(1) 把场景数据注入逻辑从 `scripts/eval_scenarios.py` 抽成可复用的 context manager；(2) 把假设标签集中到 `agent/hypotheses.py` 单一数据源，`app.py` 改为读取并加防御性兜底；(3) 给 `Orchestrator._finalize()` 加置信度地板，新增 `other_unclassified` 结论；(4) 同步更新 `BACKEND_INTEGRATION.md` 的对接文档。

**Tech Stack:** Python 3.12, pandas, pytest（现有依赖，不新增任何包）。

## Global Constraints

- `MIN_CONFIDENCE_FLOOR` 精确值为 `0.30`（与现有 `config.CONVERGE_MARGIN` 同量级，spec 中明确指定）。
- 新结论枚举值精确拼写为 `other_unclassified`（不是 `unclassified` 或其他变体）。
- `Orchestrator.final["ranking"]` 字段必须保持只含真实跑过 probe 的 5 个假设，**不**注入一个虚构的 `other_unclassified` 打分节点。
- 不修改 `agent/curation.py`、`tools/dashboard.py` 的对外契约。
- 不实现场景选择器 UI（`app.py` 场景切换是下一轮的范围，这次 `use_scenario_data()` 只给 `scripts/eval_scenarios.py` 用）。
- 每个 Task 完成后运行 `PYTHONPATH=. python -m pytest tests/ -v` 确保全量测试仍然通过（当前基线：57 passed，运行前已执行过 `python data/generate_mock_data.py` 生成 `data/mock_loans.csv`，该文件已在 `.gitignore` 中）。

---

### Task 1: 场景数据注入的可复用 context manager

**Files:**
- Create: `data/scenario_runtime.py`
- Test: `tests/test_scenario_runtime.py`

**Interfaces:**
- Produces: `use_scenario_data(df: pandas.DataFrame)` — context manager，`with use_scenario_data(df): ...` 块内 `tools.dashboard.query_dashboard(...)` 读到的是 `df` 的内容；退出块（正常或异常）后 `config.DATA_PATH` 恢复原值、`tools.dashboard._load` 缓存被清空。供 Task 2 使用。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_scenario_runtime.py`：

```python
"""use_scenario_data()：临时替换 config.DATA_PATH 供 dashboard 读取，
退出 with 块时（含异常）必须恢复原路径、清空 dashboard 缓存。"""
import pandas as pd
import pytest

import config
import tools.dashboard as dashboard
from data.scenario_runtime import use_scenario_data


def _make_df(n_rows: int) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-07-01"] * n_rows,
        "user_id": [f"u{i}" for i in range(n_rows)],
        "channel": ["channel_A"] * n_rows,
        "city_tier": ["T1"] * n_rows,
        "age_group": ["26-35"] * n_rows,
        "credit_score": [650] * n_rows,
        "approved": [True] * n_rows,
        "loan_amount": [5000] * n_rows,
        "fpd30": [False] * n_rows,
    })


def test_use_scenario_data_points_dashboard_at_new_data(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with use_scenario_data(_make_df(7)):
        assert config.DATA_PATH != str(original_csv)
        result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
        assert result["records"][0]["value"] == 7

    assert config.DATA_PATH == str(original_csv)
    dashboard._load.cache_clear()
    result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
    assert result["records"][0]["value"] == 3


def test_use_scenario_data_restores_path_after_exception(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with pytest.raises(ValueError, match="boom"):
        with use_scenario_data(_make_df(7)):
            raise ValueError("boom")

    assert config.DATA_PATH == str(original_csv)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. python -m pytest tests/test_scenario_runtime.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'data.scenario_runtime'`

- [ ] **Step 3: 实现 `data/scenario_runtime.py`**

```python
"""场景数据注入：让 tools/dashboard.py 在一个 with 块内读到指定的 DataFrame，
而不是默认的 config.DATA_PATH。

tools/dashboard.py::_load() 用 @lru_cache(maxsize=1) 缓存全局唯一一份数据，
只换 config.DATA_PATH 不清缓存的话，第二次调用还是读旧数据；用 context manager
包一层，保证正常退出和异常退出时都会恢复原路径、清空缓存，不污染同一进程内
后续对默认数据的读取。
"""
from contextlib import contextmanager
import os
import shutil
import tempfile

import pandas as pd

import config
import tools.dashboard as dashboard


@contextmanager
def use_scenario_data(df: pd.DataFrame):
    tmp_dir = tempfile.mkdtemp(prefix="scenario_data_")
    tmp_csv = os.path.join(tmp_dir, "data.csv")
    df.to_csv(tmp_csv, index=False)
    original_path = config.DATA_PATH
    try:
        config.DATA_PATH = tmp_csv
        dashboard._load.cache_clear()
        yield
    finally:
        config.DATA_PATH = original_path
        dashboard._load.cache_clear()
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_scenario_runtime.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add data/scenario_runtime.py tests/test_scenario_runtime.py
git commit -m "feat: extract use_scenario_data() context manager for scenario data injection"
```

---

### Task 2: `scripts/eval_scenarios.py` 改用 `use_scenario_data()`

**Files:**
- Modify: `scripts/eval_scenarios.py`

**Interfaces:**
- Consumes: `use_scenario_data(df)` from Task 1 (`data/scenario_runtime.py`)。
- Produces: `run_one(run_id, df, spec) -> dict`，返回 schema 不变（`run_id` / `scenario_type` / `ground_truth` / `predicted` / `hit` / `confidence` / `runner_up_gap` / `converged` / `tool_calls_used` / `ranking`，或异常时的 `error` 键），本 plan 内无下游消费者，但要跟改动前行为一致。

- [ ] **Step 1: 记录改动前的基线输出（用于人工比对，不是自动化断言）**

Run: `PYTHONPATH=. python scripts/eval_scenarios.py > /tmp/eval_before.txt 2>&1`

- [ ] **Step 2: 精简 `run_one()`，删除内联的临时文件/缓存管理代码**

把 `scripts/eval_scenarios.py` 顶部导入区的

```python
import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

import config
import tools.dashboard as dashboard
from agent.orchestrator import Orchestrator
from data.scenarios import SCENARIOS, iter_runs
```

改成（去掉不再需要的 `shutil`/`tempfile`/`config`/`tools.dashboard`，新增 `use_scenario_data`）：

```python
import argparse
import json
import os
from datetime import datetime, timezone

from agent.orchestrator import Orchestrator
from data.scenario_runtime import use_scenario_data
from data.scenarios import SCENARIOS, iter_runs
```

把整个 `run_one()` 函数（原来手写 try/finally 管理临时目录、`config.DATA_PATH`、`dashboard._load.cache_clear()` 的那段）替换成：

```python
def run_one(run_id: str, df, spec) -> dict:
    """把场景 DataFrame 喂给 Orchestrator，对比预测结论与场景真值。
    数据注入的临时文件/缓存管理由 use_scenario_data() 负责，见 data/scenario_runtime.py。"""
    with use_scenario_data(df):
        try:
            final = Orchestrator().run(phenomenon=spec.phenomenon)
        except Exception as exc:
            return {
                "run_id": run_id, "scenario_type": spec.scenario_type,
                "ground_truth": spec.ground_truth, "error": f"{type(exc).__name__}: {exc}",
            }
        predicted = final["conclusion_id"]
        hit = (predicted == spec.ground_truth) if spec.ground_truth else None
        runner_up_gap = (
            round(final["confidence"] - final["runner_up"]["score"], 3)
            if final.get("runner_up") else round(final["confidence"], 3)
        )
        return {
            "run_id": run_id,
            "scenario_type": spec.scenario_type,
            "ground_truth": spec.ground_truth,
            "predicted": predicted,
            "hit": hit,
            "confidence": final["confidence"],
            "runner_up_gap": runner_up_gap,
            "converged": final["converged"],
            "tool_calls_used": final["tool_calls_used"],
            "ranking": final["ranking"],
        }
```

`_print_table()` 和 `main()` 不需要改动。

- [ ] **Step 3: 回归验证——跑一遍并与改动前的输出比对**

Run: `PYTHONPATH=. python scripts/eval_scenarios.py > /tmp/eval_after.txt 2>&1 && diff /tmp/eval_before.txt /tmp/eval_after.txt`
Expected: 除了 `eval_results/<timestamp>.json` 那一行文件名（时间戳不同）之外，其余每一行（13 个场景的 场景/真值/预测/命中/置信度/次优分差/工具调用 + 准确率汇总）完全一致——证明重构没有改变行为，只是换了实现方式。

- [ ] **Step 4: 跑全量测试确认没有破坏其他部分**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（59 passed：Task 1 新增 2 个 + 原有 57 个）

- [ ] **Step 5: 提交**

```bash
git add scripts/eval_scenarios.py
git commit -m "refactor: eval_scenarios.py reuses use_scenario_data() instead of inline try/finally"
```

---

### Task 3: 集中假设标签到 `agent/hypotheses.py`

**Files:**
- Modify: `agent/hypotheses.py`
- Modify: `tests/test_hypotheses.py`

**Interfaces:**
- Produces: `HYP_LABELS: dict[str, str]`，key 覆盖 `build_hypothesis_tree()` 产出的全部 `hyp_id`（`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue` / `regulatory_policy_change`）外加 `other_unclassified`（为 Task 5 提前预留，此时还不会被触发）。供 Task 4（`app.py`）使用。

- [ ] **Step 1: 写失败测试**

在 `tests/test_hypotheses.py` 末尾追加：

```python
def test_hyp_labels_cover_all_tree_hypotheses_plus_fallback():
    from agent.hypotheses import HYP_LABELS
    tree = build_hypothesis_tree()
    assert set(tree) <= set(HYP_LABELS)
    assert "other_unclassified" in HYP_LABELS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. python -m pytest tests/test_hypotheses.py -v`
Expected: FAIL，`ImportError: cannot import name 'HYP_LABELS'`

- [ ] **Step 3: 在 `agent/hypotheses.py` 里加 `HYP_LABELS`**

在 `build_hypothesis_tree()` 函数结尾（`return tree` 那一行）之后、`# ── 证据规则表 ──` 注释之前插入：

```python
# ── 展示标签 ─────────────────────────────────────────
# 跟假设树的存在维护在同一个文件里，避免 app.py 之类的下游消费者
# 各自手抄一份、加新假设时忘记同步（历史上发生过：app.py 曾经漏掉
# regulatory_policy_change，导致该假设胜出时直接 KeyError 崩溃）。
HYP_LABELS = {
    "channel_quality_decline": "渠道质量下滑",
    "approval_policy_change": "审批口径变化",
    "macro_seasonality": "宏观季节性",
    "data_quality_issue": "数据质量问题",
    "regulatory_policy_change": "外部监管/行业政策",
    "other_unclassified": "未归类（证据不足）",
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_hypotheses.py -v`
Expected: PASS（4 passed：原有 3 个 + 新增 1 个）

- [ ] **Step 5: 提交**

```bash
git add agent/hypotheses.py tests/test_hypotheses.py
git commit -m "feat: centralize hypothesis display labels as HYP_LABELS"
```

---

### Task 4: `app.py` 改用 `HYP_LABELS`

**Files:**
- Modify: `app.py`

**Interfaces:**
- Consumes: `HYP_LABELS` from Task 3 (`agent/hypotheses.py`)。

- [ ] **Step 1: 删除本地 `HYP_LABEL` 字典，改为 import**

把 `app.py` 里的

```python
import config
from tools.dashboard import query_dashboard
from agent.orchestrator import Orchestrator
```

改成

```python
import config
from tools.dashboard import query_dashboard
from agent.orchestrator import Orchestrator
from agent.hypotheses import HYP_LABELS
```

删除整段

```python
HYP_LABEL = {
    "channel_quality_decline": "渠道质量下滑",
    "approval_policy_change": "审批口径变化",
    "macro_seasonality": "宏观季节性",
    "data_quality_issue": "数据质量问题",
}
```

- [ ] **Step 2: 把 5 处 `HYP_LABEL[...]` 直接下标访问改成 `.get(id, id)` 兜底**

逐处替换（保持其余代码不变）：

1. `f"🛠 {tag}{HYP_LABEL[step['hypothesis']]} ｜ ..."` → `f"🛠 {tag}{HYP_LABELS.get(step['hypothesis'], step['hypothesis'])} ｜ ..."`
2. `f"... → {HYP_LABEL[ev['hypothesis']]}"` → `f"... → {HYP_LABELS.get(ev['hypothesis'], ev['hypothesis'])}"`
3. `{"假设": HYP_LABEL[h["hyp_id"]], "置信度": h["score"]}` → `{"假设": HYP_LABELS.get(h["hyp_id"], h["hyp_id"]), "置信度": h["score"]}`
4. `f"### 最终归因：**{HYP_LABEL[final['conclusion_id']]}**..."` → `f"### 最终归因：**{HYP_LABELS.get(final['conclusion_id'], final['conclusion_id'])}**..."`
5. `st.markdown(f"**{HYP_LABEL[h['hyp_id']]}** — 总分 {h['score']:.3f}")` → `st.markdown(f"**{HYP_LABELS.get(h['hyp_id'], h['hyp_id'])}** — 总分 {h['score']:.3f}")`

- [ ] **Step 3: 确认没有遗留引用，且语法正确**

Run: `grep -n "HYP_LABEL\[" app.py`
Expected: 无输出（全部改成 `.get()` 形式）

Run: `python -m py_compile app.py`
Expected: 无输出、退出码 0

- [ ] **Step 4: 手动启动 Streamlit，验证界面正常**

Run: `streamlit run app.py`（会打开浏览器 `http://localhost:8501`）

在浏览器里：
1. 确认页面顶部三个 metric 卡片正常显示（基线期/异常期 FPD30、异常期起点）。
2. 点击"开始排查"按钮。
3. 确认排查过程的 expander 列表正常展开，标签显示中文（如"渠道质量下滑"），不是英文 id 或报错。
4. 确认第③屏"归因结论"正常渲染，标题里的假设名称是中文。
5. 关掉 Streamlit 进程（终端 `Ctrl+C`）。

- [ ] **Step 5: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（59 passed，`app.py` 不在测试覆盖范围内，这一步是确认改动没有间接影响其他模块）

- [ ] **Step 6: 提交**

```bash
git add app.py
git commit -m "fix: app.py reads centralized HYP_LABELS with defensive fallback instead of stale local dict"
```

---

### Task 5: 置信度地板 + `other_unclassified` 结论

**Files:**
- Modify: `config.py`
- Modify: `agent/orchestrator.py`
- Create: `tests/test_orchestrator.py`
- Modify: `tests/test_smoke.py`
- Modify: `scripts/eval_scenarios.py`（仅更新头部文档字符串，不改代码逻辑）

**Interfaces:**
- Produces: `config.MIN_CONFIDENCE_FLOOR: float = 0.30`；`Orchestrator.final` 新增 `low_confidence: bool` 键；`final["conclusion_id"]` 可能取值新增 `"other_unclassified"`。供 Task 6（`agent/curation.py` 测试）消费其行为。

- [ ] **Step 1: 写失败测试（`config.MIN_CONFIDENCE_FLOOR` + `_finalize()` 行为）**

创建 `tests/test_orchestrator.py`：

```python
"""Orchestrator._finalize() 的置信度地板：分数不够时不强行归类为已知假设，
而是返回 other_unclassified。直接操纵 tree 上的分数来构造场景，
不跑完整的 probe/工具调用流程（那部分已由 test_smoke.py 端到端覆盖）。"""
from agent.orchestrator import Orchestrator


def _finalized(**score_overrides):
    orch = Orchestrator()
    orch.anomaly_type = "metric_deviation"
    orch.direction = "UP"
    for hyp_id, score in score_overrides.items():
        orch.tree[hyp_id].score = score
    orch._finalize()
    return orch.final


def test_finalize_returns_known_hypothesis_when_above_floor():
    final = _finalized(channel_quality_decline=0.65, approval_policy_change=0.05)
    assert final["conclusion_id"] == "channel_quality_decline"
    assert final["low_confidence"] is False


def test_finalize_returns_other_unclassified_when_all_scores_below_floor():
    final = _finalized(
        channel_quality_decline=0.05, approval_policy_change=0.05,
        macro_seasonality=0.05, data_quality_issue=0.05,
        regulatory_policy_change=0.05,
    )
    assert final["conclusion_id"] == "other_unclassified"
    assert final["low_confidence"] is True
    assert "证据不足" in final["conclusion"]


def test_finalize_ranking_still_shows_real_scores_when_unclassified():
    final = _finalized(
        channel_quality_decline=0.05, approval_policy_change=0.05,
        macro_seasonality=0.05, data_quality_issue=0.05,
        regulatory_policy_change=0.05,
    )
    assert len(final["ranking"]) == 5
    assert all(h["score"] == 0.05 for h in final["ranking"])
    assert {h["hyp_id"] for h in final["ranking"]} == {
        "channel_quality_decline", "approval_policy_change", "macro_seasonality",
        "data_quality_issue", "regulatory_policy_change",
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL，`AttributeError: 'Orchestrator' object has no attribute 'low_confidence'` 或 `KeyError: 'low_confidence'`（因为 `config.MIN_CONFIDENCE_FLOOR` 还不存在/`_finalize()` 还没实现地板逻辑，具体报错以实际跑出为准，只要不是 PASS 都算符合预期）

- [ ] **Step 3: 在 `config.py` 里加 `MIN_CONFIDENCE_FLOOR`**

在

```python
MAX_TOOL_CALLS = 8        # 整个排查过程最多调用工具次数
MAX_EXPAND_PER_ROUND = 2  # 每轮最多展开的子节点数
CONVERGE_MARGIN = 0.30    # 最高分假设领先第二名多少即收敛
MAX_ROUNDS = 6            # 主循环最大轮数
```

后面加一行：

```python
MAX_TOOL_CALLS = 8        # 整个排查过程最多调用工具次数
MAX_EXPAND_PER_ROUND = 2  # 每轮最多展开的子节点数
CONVERGE_MARGIN = 0.30    # 最高分假设领先第二名多少即收敛
MAX_ROUNDS = 6            # 主循环最大轮数
MIN_CONFIDENCE_FLOOR = 0.30  # 最高分假设本身低于此值，不强行归类，返回 other_unclassified
```

- [ ] **Step 4: 改 `agent/orchestrator.py::_finalize()`**

把现有的

```python
    def _finalize(self):
        r = self._ranked()
        specialized = self.anomaly_type in self.SPECIALIZED_ANOMALY_TYPES
        self.final = {
            "conclusion": r[0].description,
            "conclusion_id": r[0].hyp_id,
            "confidence": r[0].score,
            "runner_up": {"desc": r[1].description, "score": r[1].score} if len(r) > 1 else None,
            "converged": self.converged,
            "tool_calls_used": self.tool_calls,
            "budget": config.MAX_TOOL_CALLS,
            "anomaly_type": self.anomaly_type,
            "direction": self.direction,
            "specialized": specialized,
            "note": None if specialized else (
                f"当前假设树针对指标数值类异常（metric_deviation）设计，"
                f"anomaly_type={self.anomaly_type} 尚无专属假设树，结论仅供参考"
            ),
            "ranking": [{"hyp_id": h.hyp_id, "desc": h.description, "score": h.score,
                         "evidence": h.evidence_log} for h in r],
        }
```

替换成：

```python
    def _finalize(self):
        r = self._ranked()
        top = r[0]
        low_confidence = top.score < config.MIN_CONFIDENCE_FLOOR
        specialized = self.anomaly_type in self.SPECIALIZED_ANOMALY_TYPES
        self.final = {
            "conclusion": ("证据不足以归入已知归因类型，建议人工复核" if low_confidence
                            else top.description),
            "conclusion_id": "other_unclassified" if low_confidence else top.hyp_id,
            "confidence": top.score,
            "low_confidence": low_confidence,
            "runner_up": {"desc": r[1].description, "score": r[1].score} if len(r) > 1 else None,
            "converged": self.converged,
            "tool_calls_used": self.tool_calls,
            "budget": config.MAX_TOOL_CALLS,
            "anomaly_type": self.anomaly_type,
            "direction": self.direction,
            "specialized": specialized,
            "note": None if specialized else (
                f"当前假设树针对指标数值类异常（metric_deviation）设计，"
                f"anomaly_type={self.anomaly_type} 尚无专属假设树，结论仅供参考"
            ),
            "ranking": [{"hyp_id": h.hyp_id, "desc": h.description, "score": h.score,
                         "evidence": h.evidence_log} for h in r],
        }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_orchestrator.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 更新 `tests/test_smoke.py` 的 `VALID_CONCLUSIONS`**

现有集合漏掉了 `regulatory_policy_change`（历史遗留，跟 `app.py` 的 `HYP_LABEL` 是同一类"新增假设后忘记同步"问题），这次顺带补上，并加入新的 `other_unclassified`：

```python
VALID_CONCLUSIONS = {
    "channel_quality_decline",
    "approval_policy_change",
    "macro_seasonality",
    "data_quality_issue",
    "regulatory_policy_change",
    "other_unclassified",
}
```

- [ ] **Step 7: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（63 passed：59 + Task 5 新增 3 个 `test_orchestrator.py` + `test_smoke.py` 改动不增加用例数）

- [ ] **Step 8: 真实回归验证 + 更新 `scripts/eval_scenarios.py` 头部文档**

Run: `PYTHONPATH=. python scripts/eval_scenarios.py --scenario ambiguous_negative`

预期看到 `ambiguous_negative_s42` 这一行的"预测"列从改动前的 `macro_seasonality` 变成 `other_unclassified`，"置信度"列数值不变（改动前实测为 `0.274`，因为置信度地板只改变 `_finalize()` 怎么呈现结论，不改变打分逻辑本身）。

把 `scripts/eval_scenarios.py` 头部文档字符串里最后一段（从"实测证据：`ambiguous_negative` 场景..."开始，到"...应该对照 `ambiguous_negative` 在同一假设上的置信度一起看。"结束）替换成：

```
实测证据：`ambiguous_negative` 场景在数据上完全没有结构性信号，MIN_CONFIDENCE_FLOOR（见
config.py，当前 0.30）引入前会被包装成 `macro_seasonality` 结论，置信度 0.274——跟三个
真正的 `macro_seasonality` 场景（置信度 0.307）几乎没有区分度。引入置信度地板后，
`_finalize()` 在最高分低于地板时返回 `other_unclassified`，`ambiguous_negative` 现在
正确报告为 other_unclassified（置信度数值不变，只是不再包装成一个看似确定的已知归因）。
"macro_seasonality" 3/3 全对的准确率数字本身没有变化（`ambiguous_negative` 的 ground_truth
本来就是 None，不参与准确率计算），但决策呈现层面的误导已经修正。
```

用实际跑出的置信度数值核对一遍上面这段文字里的 `0.274`，如果和本地跑出来的不一致，以本地实测值为准改文档。

- [ ] **Step 9: 提交**

```bash
git add config.py agent/orchestrator.py tests/test_orchestrator.py tests/test_smoke.py scripts/eval_scenarios.py
git commit -m "feat: add confidence floor, return other_unclassified for low-evidence conclusions"
```

---

### Task 6: 验证 `other_unclassified` 不会误入案例库

**Files:**
- Modify: `tests/test_curation.py`

**Interfaces:**
- Consumes: `"other_unclassified"` conclusion_id 语义（来自 Task 5），`agent.curation.evaluate_admission()`（已存在，不改代码）。

- [ ] **Step 1: 写测试**

在 `tests/test_curation.py` 的 `# ── evaluate_admission：门槛 + 三档决策 ──` 区块里追加：

```python
def test_evaluate_admission_rejects_unclassified_conclusion():
    # MIN_CONFIDENCE_FLOOR（0.30，agent/orchestrator.py 里用于标记 other_unclassified）
    # 低于 curation 自己的入库门槛 MIN_CASE_CONFIDENCE（0.5，config.py）——
    # 这条测试锁定"other_unclassified 结论天然过不了入库门槛"这个前提，
    # 不需要改 agent/curation.py 本身。
    final = _make_final(winner_hyp="other_unclassified", confidence=0.10, runner_up_score=0.05)
    result = curation.evaluate_admission("某异常现象", final, [])
    assert result["decision"] == "reject"
    assert result["gates"]["confidence_floor"] is False
```

- [ ] **Step 2: 跑测试确认通过（不需要改产品代码，只是新增断言）**

Run: `PYTHONPATH=. python -m pytest tests/test_curation.py -v`
Expected: PASS，包含新的 `test_evaluate_admission_rejects_unclassified_conclusion`

- [ ] **Step 3: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（64 passed）

- [ ] **Step 4: 提交**

```bash
git add tests/test_curation.py
git commit -m "test: lock in that other_unclassified conclusions can't reach case curation"
```

---

### Task 7: 更新 `BACKEND_INTEGRATION.md`

**Files:**
- Modify: `BACKEND_INTEGRATION.md`

**Interfaces:**
- 无代码接口，纯文档同步 Task 5 引入的新枚举值。

- [ ] **Step 1: 更新 HTTP 契约部分（约第 88 行）**

把

```
- `conclusionId` 目前固定五选一：`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue` / `regulatory_policy_change`，是针对"逾期率类指标异常"设计的假设池。
```

改成

```
- `conclusionId` 目前固定六选一：`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue` / `regulatory_policy_change` / `other_unclassified`。前五个是针对"逾期率类指标异常"设计的假设池；最后一个 `other_unclassified` 是新增的置信度兜底——当排查结束时最高分假设的置信度低于地板值（`config.MIN_CONFIDENCE_FLOOR`，当前 0.30）时返回，代表现有证据不足以支撑任何一个已知归因，`confidence` 字段依然会带上实际的（偏低的）分数。建议 Java/前端侧对这个值展示"需要人工介入"，不要当成和其余五个一样的确定性结论直接渲染。
```

- [ ] **Step 2: 更新 Python 函数级映射部分（约第 178-179 行）**

把

```
`conclusion_id` 目前固定五选一（`channel_quality_decline` / `approval_policy_change` /
`macro_seasonality` / `data_quality_issue` / `regulatory_policy_change`），是针对"指标数值类"异常设计的假设池。
```

改成

```
`conclusion_id` 目前固定六选一（`channel_quality_decline` / `approval_policy_change` /
`macro_seasonality` / `data_quality_issue` / `regulatory_policy_change` / `other_unclassified`）。
前五个是针对"指标数值类"异常设计的假设池；`other_unclassified` 是置信度低于地板值
（`config.MIN_CONFIDENCE_FLOOR`）时的兜底结论，`final["low_confidence"]` 会同步置为 `True`。
```

- [ ] **Step 3: 校对**

Run: `grep -n "五选一\|六选一\|other_unclassified" BACKEND_INTEGRATION.md`
Expected: 两处"六选一"、至少两处 `other_unclassified`，不再出现"五选一"字样。

- [ ] **Step 4: 提交**

```bash
git add BACKEND_INTEGRATION.md
git commit -m "docs: document other_unclassified conclusionId for Java backend integration"
```

---

## 完成后整体验证

- [ ] `PYTHONPATH=. python -m pytest tests/ -v` 全部通过（预期 64 passed）
- [ ] `PYTHONPATH=. python scripts/eval_scenarios.py` 跑完全部 13 个场景无报错
- [ ] `streamlit run app.py` 能正常跑完一次排查、界面无 `KeyError`
