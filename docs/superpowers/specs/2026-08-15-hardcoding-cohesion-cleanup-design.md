# 硬编码清理 + 归因结论置信度兜底 设计

## 背景

三处已确认的问题：

1. `scripts/eval_scenarios.py::run_one()` 里手写了一段 try/finally：把场景 DataFrame 写成临时 CSV、替换 `config.DATA_PATH`、清空 `tools.dashboard._load()` 的 `@lru_cache(maxsize=1)`（无参全局单例缓存，不清的话第二个场景开始会一直读第一个场景的数据）、跑完再恢复原值。这段逻辑目前只写在这一个脚本里，是内联代码而不是可复用组件。
2. `app.py` 里的 `HYP_LABEL` 字典是手写的，只有 4 条（`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue`），跟 `agent/hypotheses.py::build_hypothesis_tree()` 实际产出的 5 个假设（漏了 `regulatory_policy_change`）不同步。一旦 `regulatory_policy_change` 成为最终结论或出现在证据链里，`app.py` 里 `HYP_LABEL[step['hypothesis']]` 这类直接下标访问会抛 `KeyError`，界面直接崩溃。
3. `agent/orchestrator.py::_finalize()` 目前无论证据多薄弱，最终结论必然是 5 个假设里分数最高的那个，没有"证据不足、拒绝分类"的机制。`scripts/eval_scenarios.py` 顶部注释已经记录过一个真实案例：`ambiguous_negative`（无结构性异常信号的负例）场景下，agent 仍给出 `macro_seasonality` 结论，置信度 0.273——跟三个真正的 `macro_seasonality` 场景（置信度 0.307）几乎没有区分度，是一次没暴露出来的误判。

此外，`BACKEND_INTEGRATION.md` 把 `conclusionId` 文档成"目前固定五选一"，是 Java 网关侧依赖的既有契约，这次如果给结论加一个新的枚举值，需要同步更新这份对接文档。

## 目标

1. 把"切换场景数据源 + 清缓存"的逻辑从 `scripts/eval_scenarios.py` 内联代码抽成独立、可复用的 context manager，脚本本身改用它，消除这段逻辑目前散落在一处、以后想在别处复用还要重新抄一遍的问题。
2. 把 `app.py` 里写死且已经跟 `agent/hypotheses.py` 不同步的 `HYP_LABEL`，改成从 `agent/hypotheses.py` 读的单一数据源，并加防御性兜底，不会再因为漏了某个 `hyp_id` 而崩溃。
3. 给 `Orchestrator` 加一个置信度地板：结论分数低于阈值时，不再强行归到 5 个假设之一，而是返回"证据不足 / 待人工复核"。
4. 同步更新 `BACKEND_INTEGRATION.md` 的 `conclusionId` 枚举文档。

## 非目标

- 不做 `app.py` 的场景选择器 UI（即原设计讨论里的"Part 2：scenario-aware demo"）。这次先不碰 `app.py` 的场景切换逻辑，只碰 `app.py` 里 `HYP_LABEL` 的用法本身；`use_scenario_data()` 这次只接给 `scripts/eval_scenarios.py` 用，暂不接入 `app.py`。
- 不改 `tools/dashboard.py` 的对外契约（不加参数、不改返回 schema）。
- 不对 `agent/hypotheses.py` 的假设池做"注册函数"式重构——现有的 dict 字面量本身已经是够用的扩展方式（加一个新假设就是加一条 dict 条目），不是这次问题的根源，问题根源是"分数低时被强行归类"，用置信度地板解决。
- 不改 `agent/llm_client.py::_mock_judgment` 的关键词匹配逻辑——这是 `MOCK_LLM=1` 时的 demo-only 兜底，README 已经作为"已知简化"记录过，不在这次范围内。
- 不改 `agent/curation.py`——已确认 `MIN_CASE_CONFIDENCE`（0.5）高于新增的 `MIN_CONFIDENCE_FLOOR`（0.30），`other_unclassified` 结论的置信度必然低于两个阈值，天然过不了案例入库的 `confidence_floor` 门槛，不需要额外改动；这次会加一条测试断言这个前提持续成立，而不只是口头保证。

## 架构

```
data/scenario_runtime.py   新增：use_scenario_data() context manager
scripts/eval_scenarios.py  改：run_one() 改用 use_scenario_data()，删除内联的 try/finally
agent/hypotheses.py        改：新增 HYP_LABELS 字典（含 other_unclassified）
app.py                     改：本地 HYP_LABEL 字典删除，改为 import HYP_LABELS，读取处加 .get(id, id) 兜底
config.py                  改：新增 MIN_CONFIDENCE_FLOOR = 0.30
agent/orchestrator.py      改：_finalize() 加置信度地板判断
BACKEND_INTEGRATION.md     改：conclusionId 枚举文档更新为六选一
```

不改动 `agent/scoring.py`、`agent/investigator.py`、`tools/dashboard.py`、`data/scenarios.py` 的现有逻辑。

## 组件详细设计

### 1. `data/scenario_runtime.py`（新增）

```python
from contextlib import contextmanager
import os
import shutil
import tempfile

import pandas as pd

import config
import tools.dashboard as dashboard


@contextmanager
def use_scenario_data(df: pd.DataFrame):
    """把 df 写成临时 CSV，让 tools.dashboard.query_dashboard() 在 with 块内读到这份数据；
    退出时（正常返回或抛异常）都会恢复 config.DATA_PATH 并清空 dashboard 的 lru_cache，
    不污染同一进程内后续对默认数据的读取。"""
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

行为跟 `scripts/eval_scenarios.py::run_one()` 里现有的 try/finally 完全等价，只是抽成独立函数、用 `@contextmanager` 语法代替调用方手写 try/finally，调用方不用记住"改路径和清缓存要成对出现、且退出时都要复原"这种细节。

### 2. `scripts/eval_scenarios.py` 改动

`run_one()` 里原来的"建临时目录 / 写 CSV / 替换路径 / 清缓存 / finally 恢复"整段代码删掉，改成：

```python
from data.scenario_runtime import use_scenario_data

def run_one(run_id: str, df, spec) -> dict:
    with use_scenario_data(df):
        try:
            final = Orchestrator().run(phenomenon=spec.phenomenon)
        except Exception as exc:
            return {
                "run_id": run_id, "scenario_type": spec.scenario_type,
                "ground_truth": spec.ground_truth, "error": f"{type(exc).__name__}: {exc}",
            }
        predicted = final["conclusion_id"]
        ...  # 对比真值、组装返回行的逻辑不变，缩进从 try 块移到 with 块
    return {...}
```

对比真值、算 `runner_up_gap`、组装返回 dict 的逻辑保持不变，只是不再需要自己管理临时文件和缓存的生命周期。

### 3. `agent/hypotheses.py` 新增 `HYP_LABELS`

```python
HYP_LABELS = {
    "channel_quality_decline": "渠道质量下滑",
    "approval_policy_change": "审批口径变化",
    "macro_seasonality": "宏观季节性",
    "data_quality_issue": "数据质量问题",
    "regulatory_policy_change": "外部监管/行业政策",
    "other_unclassified": "未归类（证据不足）",
}
```

放在 `build_hypothesis_tree()` 定义附近、同一个文件里，让"假设的存在"和"假设的展示文案"维护在一处，避免以后加新假设时改两个文件、漏掉一个。

`app.py` 改动：删除本地 `HYP_LABEL` 字典定义，改为：

```python
from agent.hypotheses import HYP_LABELS
...
# 所有原来 HYP_LABEL[step['hypothesis']] / HYP_LABEL[ev['hypothesis']] / HYP_LABEL[h['hyp_id']] / HYP_LABEL[final['conclusion_id']]
# 这类直接下标访问，统一换成：
HYP_LABELS.get(step['hypothesis'], step['hypothesis'])
```

即便以后又有新假设忘记加 label，界面上退化成显示英文 `hyp_id`，而不是直接 `KeyError` 崩溃。

### 4. 置信度地板（`config.py` + `agent/orchestrator.py`）

`config.py` 新增：

```python
MIN_CONFIDENCE_FLOOR = 0.30  # 低于此分数不强行归类为已知假设之一；跟 CONVERGE_MARGIN 用同一量级
```

`Orchestrator._finalize()` 改动：

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

`ranking` 字段保持原样——不往里面注入一个假的 `other_unclassified` 打分节点（它不是一个真正跑过 probe、参与 best-first 搜索的 `Hypothesis`）。只在 `conclusion` / `conclusion_id` / 新增的 `low_confidence` 这几个呈现层字段上做替换，UI、日志、`scripts/eval_scenarios.py` 依然能看到 5 个假设各自的真实分数，只是"最终结论该怎么呈现"变了。

`scripts/eval_scenarios.py` 的 `run_one()` 里 `predicted = final["conclusion_id"]` 这行不用改——`other_unclassified` 会自然被当成一个新的 predicted 值参与命中率统计（对 `ambiguous_negative` 这种 `ground_truth=None` 的场景，`hit` 本来就记 `n/a`，不受影响；对 4 个有真值的场景，如果地板生效导致某次运行变成 `other_unclassified`，会被记为未命中，这是符合预期的——本来就不该在证据不足时报出一个"看似命中"的结论）。

### 5. `BACKEND_INTEGRATION.md` 更新

两处"目前固定五选一"（HTTP 契约部分 + Python 函数映射部分）改成六选一，并加一段说明：

> `conclusionId` 目前固定六选一：`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue` / `regulatory_policy_change` / `other_unclassified`。最后一个是新增的置信度兜底：当排查结束时最高分假设的置信度低于地板值（0.30）时返回，代表现有证据不足以支撑任何一个已知归因，建议 Java/前端侧展示"需要人工介入"，不要当成和其余五个一样的确定性结论直接渲染。

## 数据流（置信度地板部分）

```
Orchestrator.run() 跑完所有轮次 + reflect
  → _ranked() 按分数从高到低排序 5 个假设
  → top = r[0]
  → top.score < config.MIN_CONFIDENCE_FLOOR ?
      是 → final.conclusion_id = "other_unclassified"
           final.conclusion   = 兜底提示文案
           final.low_confidence = True
      否 → final.conclusion_id = top.hyp_id（跟改动前行为一致）
           final.low_confidence = False
  → final.ranking 始终是 5 个假设的真实分数，不受这次改动影响
```

## 错误处理

- `use_scenario_data()` 的 `finally` 块保证：即使 `with` 块内 `Orchestrator().run()` 抛异常，`config.DATA_PATH` 和 `dashboard._load` 缓存也会被正确恢复，行为与改动前 `scripts/eval_scenarios.py` 内联的 try/finally 一致。
- `HYP_LABELS.get(id, id)` 不会抛异常；即便未来又有新假设忘记加 label，最坏情况是界面显示英文 `hyp_id`，而不是 `KeyError` 崩溃——这正是这次要修的问题本身。

## 测试

- `tests/test_scenario_runtime.py`（新增）：断言 `use_scenario_data()` 退出后 `config.DATA_PATH` 恢复为原值、`dashboard._load` 缓存已被清空；额外验证 `with` 块内抛异常时，退出逻辑依然正确执行（用 `pytest.raises` 包住 `with use_scenario_data(df): raise ValueError(...)`，再检查路径和缓存状态）。
- `scripts/eval_scenarios.py` 重构后手动跑一遍 `PYTHONPATH=. python scripts/eval_scenarios.py`，确认 13 个场景仍然全部正常产出结果行，行为与重构前一致（回归验证）。
- `tests/test_hypotheses.py`：新增断言 `HYP_LABELS` 的 key 集合是 `build_hypothesis_tree()` 返回的所有 `hyp_id` 的超集（覆盖当前 5 个 + `other_unclassified`），防止以后加假设时又漏写 label。
- `tests/test_curation.py`：新增断言——构造一个 `conclusion_id == "other_unclassified"`、`confidence` 低于 `MIN_CONFIDENCE_FLOOR` 的 `final` dict，调用 `evaluate_admission()`，断言 `gates["confidence_floor"]` 为 `False`、`decision == "reject"`。验证"不需要改 `agent/curation.py`"这个假设在代码层面成立，而不是只在设计文档里口头保证。
- `tests/test_orchestrator.py`（如不存在则新建）：人为构造一个所有假设都拿不到显著证据的场景（例如 mock 一个 `Orchestrator` 实例，让所有 `probe` 都不触发任何规则），断言 `final["conclusion_id"] == "other_unclassified"`、`final["low_confidence"] is True`。
- 用 `PYTHONPATH=. python scripts/eval_scenarios.py --scenario ambiguous_negative` 做一次真实回归确认：加了地板后，`scripts/eval_scenarios.py` 文件头部注释里提到的"0.273 置信度却报 macro_seasonality"这个具体案例，现在的 `conclusion_id` 应变为 `other_unclassified`。跑完后更新该文件头部那段注释，去掉已经不成立的旧描述，避免注释和代码行为不一致。

## 已知限制

- 置信度地板是全局固定阈值（0.30），复用 `CONVERGE_MARGIN` 的量级，不是针对每个假设单独校准的。5 个假设的证据权重总和不同（例如 `regulatory_policy_change` 两条规则权重和 0.60，`channel_quality_decline` 三条权重和 0.80），理论上可能出现"某个假设本身权重上限就低，即使证据全部命中也刚好卡在地板附近"的情况。这次先用统一阈值验证机制本身有效，不做逐假设校准。
- `other_unclassified` 目前只是 `_finalize()` 层的呈现层判断，`ranking` 里不会出现它自己的打分条目——它不是一个真正参与 best-first 搜索、跑过 probe 的 `Hypothesis` 节点。如果以后要做"LLM 动态生成新假设加入假设池"（brainstorming 阶段讨论过的 Approach B，这次明确不做），需要重新设计这部分，这次的地板机制不是那条路径的前置基础设施，只是权宜之计。

## 不在本次范围内（后续可能的方向）

- `app.py` 场景选择器 UI（原设计讨论中的"Part 2"，interview/demo 展示相关，后续单独一轮做）
- `_mock_judgment` 从关键词匹配改成更结构化的判断方式
- 置信度地板按假设单独校准，而不是全局统一阈值
- LLM 开放式生成新假设加入假设池（Approach B）
