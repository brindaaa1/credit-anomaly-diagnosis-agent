# 多场景 Mock 数据生成器 + Agent 评估脚本 设计

## 背景

`credit-anomaly-agent` 目前只有一份写死的模拟数据（`data/generate_mock_data.py`，固定 `seed=42`），单一故事线：`channel_C` 渠道放量导致逾期率上升，真值永远是 `channel_quality_decline`。`agent/hypotheses.py` 的 8 条证据规则、`agent/scoring.py` 的所有数值阈值（渠道占比变化 >15pp、逾期率高出均值 >50%、信用分下降 >10 分、放款笔数偏离 >40%）都是对着这一份数据手工调出来的。`tests/test_smoke.py` 也只断言"能跑完、给出合法结论"，不校验结论对不对。

结果：4 个假设分支里，只有 `channel_quality_decline` 曾经被数据验证过应该赢。其余 3 个假设从写完到现在没有被一次"真值就是它"的数据集验证过，权重/阈值是否合理无从判断，找不到优化方向。

进一步排查发现一个更严重的结构性问题：`agent/llm_client.py` 的 `_mock_judgment()`（`MOCK_LLM=1` 时的判断逻辑）里，r5（审批线反证）、r6（季节性）、r8（数据故障佐证）三条规则的返回值是硬编码常量，跟 `tool_result` 实际内容无关：

- r6 直接 `return {"triggered": False, ...}`，不看检索内容——`macro_seasonality`（唯一支撑规则就是 r6，权重 0.35）在 MOCK 模式下**结构性地永远无法被判定为真相**。
- r5 只要 `business_kb.json` 里那句静态的"最近一次调整时间为2025年3月"还在，就永远触发，无论数据是不是"审批口径变化"场景，都会给 `approval_policy_change` 倒扣 -0.30×confidence 分。
- r8 同样硬编码永远不触发。

不先修这个，多场景 eval 会测出"macro_seasonality 永远测不中""approval_policy_change 置信度系统性偏低"，但这是 MOCK 判断层的 bug，不是数值阈值/权重本身的问题，会让 eval 结果失真、误导优化方向。

## 目标

1. 把单一 mock 数据集变成参数化的多场景生成器，覆盖 4 个假设各自"真值就是它"的场景 + 同类型不同随机种子的变体 + 一个无明显信号的模糊负例。
2. 写一个 eval 脚本，批量跑 `Orchestrator.run()`，对比预测结论与场景真值，产出准确率、置信度、收敛轮次、次优假设分差的汇总表。
3. 顺带修掉两个已发现的真 bug：`_mock_judgment` 的硬编码常量返回、`tools/retrieval.py::_rank()` 空文档列表时的未防护异常。

## 非目标

- 不引入 CI 集成（先做本地手动运行的调优工具）。
- 不做 `kb/business_kb.json` / `kb/historical_cases.json` 的场景化改造——这两个文件保持静态共享。见"已知限制"一节。
- 不修改 `tools/dashboard.py` 的对外契约（不加参数、不改返回 schema），只在 eval 脚本里通过替换 `config.DATA_PATH` + 清缓存的方式喂数据，保持这层与"真实环境替换为看板 API"的约定不变。
- 不新增第 5 类场景类型（`regulatory_policy_change` 假设还在未合并的 `worktree-web-search-tool` 分支上，等它合并进 `main` 后再补）。

## 架构

```
data/scenarios.py          新增：场景化数据生成器（重构自 generate_mock_data.py）
scripts/eval_scenarios.py  新增：eval 主脚本
agent/llm_client.py        修：_mock_judgment 的 r5/r6/r8 改成真正读 tool_result
tools/retrieval.py         修：_rank() 空文档列表防御
```

不改动 `agent/orchestrator.py`、`agent/hypotheses.py`、`agent/scoring.py`、`tools/dashboard.py` 的现有逻辑——它们已经是参数化的（`Orchestrator.run()` 接受 `phenomenon`/`baseline_range`/`anomaly_range` 等），eval 脚本只是复用这些入参跑多次。

## 组件详细设计

### 1. `data/scenarios.py`（新增）

把现有 `generate_mock_data.py` 里 `sample_day()` 的逻辑拆成"公共骨架 + 场景差异参数"。公共骨架：日期范围 `2026-06-01~2026-07-30`，`ANOMALY_START = 2026-07-20`，渠道/城市层级/年龄段的取值域，逾期概率与信用分的基础负相关公式，均沿用现有实现。

```python
@dataclass
class ScenarioSpec:
    scenario_type: str          # channel_quality_decline / approval_policy_change / ...
    ground_truth: str | None    # 对应 hyp_id；ambiguous_negative 为 None
    phenomenon: str             # 传给 Orchestrator.run() 的异常描述文案
    seeds: list[int]            # 该类型下要跑几个随机种子变体

def generate(scenario_type: str, seed: int) -> pd.DataFrame: ...

SCENARIOS: dict[str, ScenarioSpec] = {...}   # 4 类 + 1 个负例

def iter_runs() -> Iterator[tuple[str, pd.DataFrame, ScenarioSpec]]:
    """产出 (run_id, dataframe, spec)，run_id 形如 channel_quality_decline_s42。"""
```

**四类场景的数据差异**（每类默认种子沿用原脚本的 `42`，另加 `101`、`202` 两个变体；`ambiguous_negative` 只需 1 个种子，不设变体）：

| scenario_type | 与基线的差异 | 预期触发的数值规则 |
|---|---|---|
| `channel_quality_decline` | 现有故事线不变：`channel_C` 占比从 ~10% 涨到 ~35%，该渠道 `credit_score` 均值低 50 分，逾期概率 +9pp | r1（占比变化>15pp）、r2（渠道逾期率高出均值>50%） |
| `approval_policy_change` | 审批线（cutoff）从 580 分降到 560 分（仅异常期），渠道占比、`credit_score` 生成分布本身不变 | r4（放款用户均分较基线降>10分）——因为新纳入 560~580 分段用户拉低整体均值 |
| `macro_seasonality` | 渠道占比、审批线、`credit_score` 分布全部不变；异常期内**所有渠道**的逾期概率整体 +5pp（无渠道结构性差异） | r6（依赖修复后的 mock judgment 识别"全渠道同步"措辞，非渠道级差异） |
| `data_quality_issue` | 逾期概率/信用分/渠道结构全程不变（真实指标平稳）；异常期内随机挑一天，该日放款记录整体丢弃 50%（模拟 ETL 当日分母缺失） | r7（放款笔数单日偏离均值>40%） |
| `ambiguous_negative` | 全程使用基线期同一套生成参数，不设置 `is_anomaly` 分支——异常期和基线期指标只有随机噪声差异，没有结构性变化 | 预期都不触发或触发但分差不足以收敛 |

具体的偏移量（cutoff 降多少、macro 加多少 pp、ETL 丢弃比例）以"能稳定触发对应数值规则、同时不误触发别的规则"为验收标准，实现时用现有 `sample_day()` 同款正态分布参数推算，跑一次 `python data/scenarios.py --preview <scenario_type>` 打印每日聚合指标验证量级（这个预览命令是实现阶段的调试手段，不是产品功能）。

### 2. `agent/llm_client.py` 的 `_mock_judgment` 修复

保持函数签名和整体分支结构（按 question 关键词路由到对应判断逻辑），只把每个分支内部"忽略 `tool_result`、返回硬编码值"的部分改成"读 `hits_text` 的实际内容再判断"：

- r6（季节性）：检查 `hits_text` 里是否同时出现"全渠道"/"整体"类描述 **且** 不含单一渠道措辞，而不是无条件 `return False`。
- r8（数据故障）：检查 `hits_text` 是否包含"当日""单日""ETL"等指向"当日分母缺失"的措辞，命中才 `triggered=True`。
- r5（审批线反证）：保留现有"是否命中变更时间"判断逻辑（这条本身有读 `hits_text`），但确认它不是恒真——已经是 `"2025年3月" in hits_text`，问题在于 `business_kb.json` 内容本身静态导致恒真，属于"已知限制"一节的范畴，不在这次改动里动 KB 内容。

修复原则：三条分支都必须在"给不同的 `tool_result` 输入"时产出不同的 `triggered` 值——这是本次要补的单测要断言的核心行为。

### 3. `scripts/eval_scenarios.py`（新增）

```python
def run_one(run_id: str, df: pd.DataFrame, spec: ScenarioSpec) -> dict:
    """写临时 CSV -> 替换 config.DATA_PATH -> 清 dashboard 缓存
    -> 构造新 Orchestrator -> run() -> 对比真值 -> 返回结果行"""

def main(scenario_filter: str | None, out_dir: str): ...
```

流程：

1. 对 `data.scenarios.iter_runs()` 产出的每个场景，把 DataFrame 写到临时目录（如 `data/.eval_tmp/<run_id>.csv`，运行结束清理或加进 `.gitignore`）。
2. 猴子补丁式替换 `config.DATA_PATH = <临时路径>`，调用 `tools.dashboard._load.cache_clear()`（`@lru_cache(maxsize=1)` 是无参缓存，不清会一直用第一个场景的数据）。
3. `Orchestrator().run(phenomenon=spec.phenomenon, anomaly_range=..., ...)`，参数取自 `ScenarioSpec` 里跟场景配套的措辞/时间范围（沿用现有默认的 `2026-06-01~2026-07-19` 基线期、`2026-07-20~2026-07-30` 异常期即可，不用每个场景单独定制日期）。
4. 记录一行结果：`run_id / scenario_type / ground_truth / predicted(conclusion_id) / hit(bool) / confidence / runner_up_gap / converged / tool_calls_used / rounds`。`ambiguous_negative` 没有 `ground_truth`，`hit` 列记为 `n/a`，额外标注 `confidence` 是否超过一个"可能误判"的参考阈值（如 0.3）。
5. 单个场景跑挂了（异常）不中断整批：`try/except`，该行标记 `error` 及异常信息，继续下一个。
6. 全部跑完后：终端打印表格（含总准确率 `命中数/有真值场景数`），同时把完整结果（含每个场景的 `ranking` 明细，即每个假设的最终分数和 evidence_log）写入 `eval_results/<timestamp>.json`。

CLI：`PYTHONPATH=. python scripts/eval_scenarios.py [--scenario channel_quality_decline] [--out eval_results/]`，不传 `--scenario` 默认跑全部 13 个。

### 4. `tools/retrieval.py::_rank()` 修复

```python
def _rank(query: str, docs: list[dict], text_key: str, top_k: int) -> list[dict]:
    if not docs:
        return []
    ...  # 原逻辑不变
```

## 数据流

```
data/scenarios.py 生成 DataFrame（按 scenario_type + seed）
  → scripts/eval_scenarios.py 写临时 CSV，替换 config.DATA_PATH，清 dashboard 缓存
  → Orchestrator.run(phenomenon=..., anomaly_range=...)
  → 收集 final["conclusion_id"] / confidence / ranking
  → 写入终端表格 + eval_results/<timestamp>.json
```

## 错误处理

- 单场景运行异常：捕获并记录为 `error` 行，不中断整批 eval。
- 临时 CSV 写入失败（如磁盘只读）：直接抛出，中断整个 eval（这是环境问题，不该静默跳过）。
- `config.DATA_PATH` 替换后必须在 `finally` 里恢复原值，避免 eval 跑完后污染后续手动调试（如直接 `python agent/orchestrator.py`）时用的默认数据路径。

## 测试

- `tests/test_retrieval.py`（新增或追加）：`_rank([], ...)` 返回 `[]`，不抛异常。
- `tests/test_llm_client.py`（追加）：给 r5/r6/r8 各构造"应该触发"和"不应该触发"两种 `tool_result`，断言 `triggered` 随内容变化（而不是恒定值）。
- `data/scenarios.py` 里每个 `generate()` 补一个轻量单测：断言生成的 DataFrame 在对应数值规则上确实会触发（如 `approval_policy_change` 场景生成后，异常期均分比基线期低 >10 分），避免"参数量级不够、规则测不出信号"这种静默失效。
- `scripts/eval_scenarios.py` 不写单元测试（手动调优工具，非产品代码），但作为验收标准，13 个场景必须全部正常产出结果行（不代表全部预测命中）。

## 已知限制

- `kb/business_kb.json`、`kb/historical_cases.json` 保持静态，不随场景变化。r5/r8 这类依赖 KB 内容的规则，测的是"agent 有没有正确响应检索到的内容"，不是"KB 内容本身是否匹配当前场景真值"。例如 `approval_policy_change` 场景里，KB 仍然说"最近一次调整时间为2025年3月"，r5 大概率仍会触发反证、拉低该假设分数——这是本次改动后依然存在的系统性偏差，不是这次要解决的问题。eval 输出的 JSON 里会给每条规则标注 `judge_type`（已有字段），方便后续单独筛出 KB 依赖型规则的表现。
- `p_ch_case` 探测（历史案例检索，query 固定为"渠道放量"相关文案）在所有场景下都可能命中 `case_2025_11`（渠道类案例），因为查询文本不随场景变化——这会给 `channel_quality_decline` 假设在非渠道类场景里也带来一点先验加分。这次不改动 probe 的 query 生成逻辑（超出本次范围），但会体现在 eval 结果的 evidence_log 里，可作为下一轮优化的候选项。

## 不在本次范围内（后续可能的方向）

- KB/历史案例库场景化
- probe 的 query 生成从模板拼接改成随场景动态生成
- 接入 CI 作为回归门槛
- 第 5 类假设 `regulatory_policy_change` 的场景（等 web-search 分支合并后再补）
