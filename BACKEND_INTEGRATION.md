# 对接说明（给 Java 后端同学）

Python 这边已经把 HTTP 接口层（`api.py`）写好了，Java 只需要发 HTTP 请求，
不需要读 Python 代码。本文档第一部分是 HTTP 契约，直接照着调即可；
第二部分是给"以后想直接 import Python 函数"的场景留的参考，可以跳过不看。

## 启动服务

```bash
pip install -r requirements.txt
python data/generate_mock_data.py     # 生成 mock 数据，只需跑一次
uvicorn api:app --host 0.0.0.0 --port 8000
```

默认 `MOCK_LLM=1`（无需 API key）。接真实 LLM 判断需要 `export MOONSHOT_API_KEY=... && export MOCK_LLM=0`。

## HTTP 接口

### ① 提交异常事件

```
POST /api/v1/diagnoses
Content-Type: application/json
```

请求体就是 Java 现有的异常事件结构，原样发送即可：

```json
{
  "eventId": "anomaly_20260720_fpd30_001",
  "metricName": "fpd30_rate",
  "detectedAt": "2026-07-21T08:00:00+08:00",
  "anomalyType": "metric_deviation",
  "direction": "UP",
  "baselineRange": {"start": "2026-06-01", "end": "2026-07-19"},
  "anomalyRange": {"start": "2026-07-20", "end": "2026-07-30"},
  "suspectSegments": [],
  "phenomenon": "某新接入渠道新增用户FPD30逾期率单周环比上升25%，多头占比明显高于大盘"
}
```

- `suspectSegments` 可选，不传或传空数组都行（Java 那边这个能力还没做，先留空，Python 侧已按"有则用、无则跳过"处理，以后接上不需要改接口）。
- `phenomenon` 可选，不传就用其他六个字段自动拼一句模板文本兜底。如果 Java 侧能拿到更具体的异常描述（不只是结构化字段），传这个字段能让历史案例检索匹配得明显更准——检索现在走语义 embedding，结构化字段拼出来的模板文本本身语义信息有限。
- `diagnosisId` 直接复用 `eventId`：同一个 `eventId` 重复提交是幂等的（不会重复跑，直接返回已有任务状态），方便 Java 端做重试。

响应（`202 Accepted`，立即返回，不等排查跑完）：

```json
{"diagnosisId": "anomaly_20260720_fpd30_001", "eventId": "anomaly_20260720_fpd30_001", "status": "pending"}
```

### ② 查诊断结果

```
GET /api/v1/diagnoses/{diagnosisId}
```

`status` 依次是 `pending` → `running` → `done`（或 `error`），Java 侧轮询直到拿到终态。

```json
{
  "diagnosisId": "anomaly_20260720_fpd30_001",
  "eventId": "anomaly_20260720_fpd30_001",
  "status": "done",
  "error": null,
  "conclusionId": "channel_quality_decline",
  "conclusion": "获客渠道质量下滑，新增用户本身风险更高",
  "confidence": 0.607,
  "anomalyType": "metric_deviation",
  "direction": "UP",
  "specialized": true,
  "note": null,
  "converged": true,
  "toolCallsUsed": 4,
  "budget": 8,
  "ranking": [
    {"hyp_id": "channel_quality_decline", "desc": "...", "score": 0.607, "evidence": [...]},
    "..."
  ],
  "curation": {
    "decision": "high_quality",
    "gates": {"converged": true, "specialized": true, "confidence_floor": true},
    "scores": {"R1_evidence_diversity": 1.0, "R2_margin": 1.0, "R3_reflect_stability": 1.0, "R4_novelty": 0.82},
    "admission_score": 0.94
  }
}
```

- `diagnosisId` 不存在时返回 `404`。
- `conclusionId` 目前固定六选一：`channel_quality_decline` / `approval_policy_change` / `macro_seasonality` / `data_quality_issue` / `regulatory_policy_change` / `other_unclassified`。前五个是针对"逾期率类指标异常"设计的假设池；最后一个 `other_unclassified` 是新增的置信度兜底——当排查结束时最高分假设的置信度低于地板值（`config.MIN_CONFIDENCE_FLOOR`，当前 0.30）时返回，代表现有证据不足以支撑任何一个已知归因，`confidence` 字段依然会带上实际的（偏低的）分数。建议 Java/前端侧对这个值展示"需要人工介入"，不要当成和其余五个一样的确定性结论直接渲染。
- **`specialized: false` 不是 bug**：`anomalyType` 传 `model_degradation` / `distribution_shift` 时，排查照常跑完并返回结论，但当前假设池还没有针对这两类异常的专属逻辑，`specialized` 会是 `false`，`note` 带一句中文提示。建议把这两个字段透传给前端展示，不要静默丢弃。
- `error` 非空时，`status` 会是 `error`，其余业务字段为空。
- `curation` 是这次诊断结论要不要沉淀成新案例的 rubric 打分结果（`agent/curation.py`），纯供观测/审计用，**Java 侧不需要基于它做任何逻辑判断**：`decision` 是 `high_quality`（已自动写入案例库）/ `candidate`（等人工复核）/ `reject`（未过门槛，不入库）三选一。门槛没过时 `gates` 里对应项是 `false`，`scores`/`admission_score` 会是空。

## 手动验证（不接 Java，本地模拟触发）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/diagnoses -H "Content-Type: application/json" -d '{...上面案例1的 JSON...}'
curl http://127.0.0.1:8000/api/v1/diagnoses/anomaly_20260720_fpd30_001
```

---

## 附：Python 函数级映射（可跳过，除非要直接 import 而不走 HTTP）

## 入口

```python
from agent.orchestrator import Orchestrator

final = Orchestrator().run(
    phenomenon=...,        # str，中文现象描述，喂给历史案例 RAG 检索用（见下）
    metric_name=...,       # str，如 "fpd30_rate"
    baseline_range=...,    # str，"2026-06-01~2026-07-19"（注意是波浪号拼接，不是两个字段）
    anomaly_range=...,     # str，"2026-07-20~2026-07-30"，格式同上
    direction=...,         # "UP" / "DOWN" / "SHIFT"
    anomaly_type=...,      # "metric_deviation" / "model_degradation" / "distribution_shift"
    suspect_segments=...,  # list[dict] 或 None，见下
)
```

`Orchestrator().run(...)` 是**同步阻塞调用**（内部要跑多轮工具调用 + LLM 判断），
真实接 LLM（`config.MOCK_LLM=0`）时可能到几秒到几十秒，不要在请求线程里直接同步等——
具体怎么做异步/轮询由你决定。

## 字段映射（Java 请求体 → Python 参数）

| Java 字段 | 类型 | → Python 参数 | 备注 |
|---|---|---|---|
| `eventId` | string | 不直接传入 `run()` | 建议你自己用它做任务/结果的唯一键 |
| `metricName` | string | `metric_name` | 原样传 |
| `detectedAt` | string(ISO8601) | 不使用 | Agent 不需要检测时间，只用两个 range |
| `anomalyType` | enum | `anomaly_type` | 原样传（三个枚举值原样传，不用转译） |
| `direction` | enum | `direction` | 原样传（`UP`/`DOWN`/`SHIFT`） |
| `baselineRange.start` + `.end` | string+string | `baseline_range` | **要拼接**：`f"{start}~{end}"` |
| `anomalyRange.start` + `.end` | string+string | `anomaly_range` | **要拼接**：`f"{start}~{end}"` |
| `suspectSegments` | array（Java 暂未实现） | `suspect_segments` | 现阶段不传/传空数组时给 `None` 即可 |
| — | — | `phenomenon` | Java 请求体里没有对应字段，需要你自己拼一句中文描述，见下 |

## 两个坑点

1. **`baselineRange`/`anomalyRange` 是 `{start, end}` 对象，Python 要的是一个波浪号拼接的字符串**，不做转换会直接报错或查不到数据。
2. **`suspect_segments` 内部用的是驼峰 key**：`{"dimension": ..., "value": ..., "contribution": 0.72, "baselineValue": ..., "anomalyValue": ..., "sampleSize": ...}`（历史遗留，Python 代码里没转成 snake_case）。现阶段 Java 还没实现这个能力，传 `None`/空数组即可，`run()` 会自动跳过对应逻辑，不影响其余排查。

## `phenomenon` 参数怎么拼

Agent 排查前会拿 `phenomenon` 去检索中文历史案例库（语义 embedding 匹配），结构化字段直接拼模板效果有限，**优先直接传一句真实的异常描述**（见上面 HTTP 契约里新增的可选 `phenomenon` 字段）。如果确实拿不到自然语言描述，走结构化字段兜底拼接：

```python
DIRECTION_CN = {"UP": "上升", "DOWN": "下降", "SHIFT": "偏移"}
phenomenon = f"{metric_name} 近期{DIRECTION_CN[direction]}（{anomaly_type}），" \
             f"基线期 {baseline_range}，异常期 {anomaly_range}"
```

## 返回值（`final` dict）

```python
{
    "conclusion": "获客渠道质量下滑，新增用户本身风险更高",  # 结论描述
    "conclusion_id": "channel_quality_decline",              # 结论枚举 id，六选一
    "confidence": 0.65,                                       # 置信度分数
    "runner_up": {"desc": "...", "score": 0.03} | None,       # 次优假设
    "converged": True,                                        # 是否提前收敛
    "tool_calls_used": 4,
    "budget": 8,
    "anomaly_type": "metric_deviation",                       # 原样回传，方便对账
    "direction": "UP",
    "specialized": True,   # False 表示当前假设树未针对该 anomaly_type 定制
    "note": None,          # specialized=False 时会有一句中文提示，展示给业务方用
    "ranking": [
        {"hyp_id": "channel_quality_decline", "desc": "...", "score": 0.65,
         "evidence": [...]},   # 每条假设的完整证据链，UI 展示用
        ...
    ],
}
```

`api.py` 在拿到这个 dict 后会再加一个 `curation` 键（`agent.curation.evaluate_admission()` 的返回值），见上面 HTTP 契约部分的说明，直接 import `Orchestrator` 走这条路的话不会自动带上，需要自己调用。

`conclusion_id` 目前固定六选一（`channel_quality_decline` / `approval_policy_change` /
`macro_seasonality` / `data_quality_issue` / `regulatory_policy_change` / `other_unclassified`）。
前五个是针对"指标数值类"异常设计的假设池；`other_unclassified` 是置信度低于地板值
（`config.MIN_CONFIDENCE_FLOOR`）时的兜底结论，`final["low_confidence"]` 会同步置为 `True`。

## `specialized: false` 是什么意思

`anomaly_type` 传 `model_degradation` 或 `distribution_shift` 时，排查照常跑完、正常返回结论，
但 `specialized` 会是 `False`，`note` 会带一句"当前假设树未针对该异常类型定制，结论仅供参考"。
**这不是 bug**，是因为现在只有一套面向逾期率类指标异常的假设树，其余两种异常类型还没有专属假设池
（属于后续要做的部分，接口不需要为此改动）。建议后端把 `specialized`/`note` 透传给前端展示，
不要静默丢弃。

## 跑前置条件

```bash
pip install -r requirements.txt
python data/generate_mock_data.py   # 生成 mock 数据，只需跑一次
export MOONSHOT_API_KEY=sk-xxx      # 真实 LLM 判断需要；不设置则走 MOCK_LLM=1 规则模拟
export MOCK_LLM=0
```
