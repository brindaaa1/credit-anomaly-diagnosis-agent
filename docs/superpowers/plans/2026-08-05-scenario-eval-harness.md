# 多场景 Mock 数据生成器 + Agent 评估脚本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `credit-anomaly-agent` 从"只有一个写死的 mock 场景"变成有 13 个带真值标签的合成场景 + 一个批量 eval 脚本，能真正回答"权重/阈值该往哪个方向调"这个问题。

**Architecture:** 新增 `data/scenarios.py`（参数化场景生成器）和 `scripts/eval_scenarios.py`（批量跑 `Orchestrator.run()` 对比真值），修复 `agent/llm_client.py::_mock_judgment` 里两条硬编码永不触发的分支、`tools/retrieval.py::_rank()` 的空文档列表异常。不改动 `agent/orchestrator.py`、`agent/hypotheses.py`、`agent/scoring.py`、`tools/dashboard.py`、`data/generate_mock_data.py`。

**Tech Stack:** Python，`pandas`/`numpy`（已在 `requirements.txt`），`pytest`。

## Global Constraints

- 参照设计文档 `docs/superpowers/specs/2026-08-05-scenario-eval-harness-design.md`，所有偏离该文档的决策都需要有理由。
- 不修改 `tools/dashboard.py` 的函数签名/返回 schema（真实环境会替换这层实现，契约不能变）。
- 不修改 `data/generate_mock_data.py`（保持 README 里 `python data/generate_mock_data.py` 快速开始命令的行为不变）。
- 不修改 `kb/business_kb.json`、`kb/historical_cases.json`（场景化 KB 不在本次范围）。
- 不接入 CI（`scripts/eval_scenarios.py` 是本地手动运行的调优工具）。
- 测试默认 `MOCK_LLM=1`（项目默认值，见 `config.py`），不需要真实 API key。
- 新测试文件放在 `tests/` 目录，跟现有测试同级（`tests/test_retrieval.py`、`tests/test_llm_client.py`、`tests/test_scenarios.py`），风格参照 `tests/test_curation.py`：真实数据不 mock，涉及文件路径用 `monkeypatch` 换成临时路径。
- 所有新代码里的中文注释/docstring 风格与现有代码保持一致（简体中文，解释"为什么"而不是"是什么"）。

---

## Task 1: 修复 `tools/retrieval.py::_rank()` 空文档列表异常

**Files:**
- Modify: `tools/retrieval.py:18-24`（`_rank()` 函数）
- Test: `tests/test_retrieval.py`（新增）

**Interfaces:**
- Consumes: 无（独立 bug 修复）
- Produces: `_rank(query: str, docs: list[dict], text_key: str, top_k: int) -> list[dict]`，`docs=[]` 时返回 `[]`，不抛异常（后续任务不直接依赖这个改动，但它让整个 eval 流程在案例库为空的边界情况下更健壮）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_retrieval.py`：

```python
"""tools/retrieval.py 的边界情况测试：文档列表为空时不应该抛异常
（原实现里 TfidfVectorizer().fit_transform([]) 会因为空词表报错）。"""
from tools.retrieval import _rank


def test_rank_returns_empty_list_when_docs_empty():
    assert _rank("随便查询", [], "phenomenon", top_k=3) == []


def test_rank_still_works_with_normal_docs():
    docs = [
        {"phenomenon": "渠道放量导致逾期上升", "extra": 1},
        {"phenomenon": "宏观季节性波动", "extra": 2},
    ]
    hits = _rank("渠道放量 逾期", docs, "phenomenon", top_k=3)
    assert len(hits) >= 1
    assert hits[0]["phenomenon"] == "渠道放量导致逾期上升"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_retrieval.py -v`
Expected: `test_rank_returns_empty_list_when_docs_empty` FAIL，报 `ValueError`（空词表）或类似 TF-IDF 相关异常；`test_rank_still_works_with_normal_docs` 应该已经 PASS（验证没改坏正常路径）。

- [ ] **Step 3: 修复实现**

编辑 `tools/retrieval.py`，在 `_rank()` 函数体最前面加空列表提前返回：

```python
def _rank(query: str, docs: list[dict], text_key: str, top_k: int) -> list[dict]:
    if not docs:
        return []
    corpus = [_tokenize(d[text_key]) for d in docs]
    vec = TfidfVectorizer()
    mat = vec.fit_transform(corpus + [_tokenize(query)])
    sims = cosine_similarity(mat[-1], mat[:-1]).flatten()
    ranked = sorted(zip(sims, docs), key=lambda x: -x[0])[:top_k]
    return [{**d, "similarity": round(float(s), 3)} for s, d in ranked if s > 0]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_retrieval.py -v`
Expected: 两个测试都 PASS。

- [ ] **Step 5: 跑一次全量回归，确认没改坏别的**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest -q`
Expected: 全部通过（原有测试 + 新增的 2 个）。

- [ ] **Step 6: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add tools/retrieval.py tests/test_retrieval.py
git commit -m "fix: _rank() returns [] for empty doc list instead of crashing"
```

---

## Task 2: 修复 `agent/llm_client.py::_mock_judgment` 的硬编码分支（r6/r8）

**Files:**
- Modify: `agent/llm_client.py:55-78`（`_mock_judgment()` 函数）
- Test: `tests/test_llm_client.py`（新增）

**Interfaces:**
- Consumes: 无
- Produces: `_mock_judgment(question: str, tool_result: dict) -> dict`（返回 `{"triggered": bool, "confidence": float, "reasoning": str}`）——r6（季节性）、r8（数据故障）两条分支的 `triggered` 值必须随 `tool_result["hits"]` 内容变化；r5（审批线反证）、r3（渠道案例相似）分支保持不变（它们已经是读 `hits_text` 判断，只是受限于静态 KB 内容，不在本次修复范围）

**背景**：现状 r6 分支直接 `return {"triggered": False, ...}`，不看 `tool_result`；r8 分支同样硬编码 `triggered: False`。这导致 `macro_seasonality`（唯一支撑规则是 r6）在 MOCK 模式下结构性地永远无法被判定为真相，`data_quality_issue` 的 r8 佐证也永远不生效。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_llm_client.py`：

```python
"""_mock_judgment 的 r6（季节性）/r8（数据故障）分支必须依据 tool_result
实际内容判断，不能是无视输入的硬编码常量。r5（审批线反证）分支已经是
读 hits_text 判断，这里补一个回归测试锁定现有正确行为。"""
from agent.llm_client import _mock_judgment


def _hits(*phenomena):
    return {"hits": [{"phenomenon": p} for p in phenomena]}


def test_r6_seasonality_triggers_on_全渠道整体性_pattern():
    question = ("检索到的历史案例中，是否有证据表明当前时间段（7月下旬）历史上存在"
                "规律性、全渠道整体性的逾期抬升？注意区分'单一渠道异常'和'全渠道整体抬升'。")

    hit = _mock_judgment(question, _hits("全渠道逾期率在春节后两周整体抬升，无单一渠道异常"))
    assert hit["triggered"] is True

    miss = _mock_judgment(question, _hits("某新接入渠道占比上升，逾期率显著高于其他渠道"))
    assert miss["triggered"] is False


def test_r8_data_quality_triggers_on_etl_pattern():
    question = ("根据知识库中的口径说明，当前'逾期率持续多日偏高（而非单日跳变后恢复）'的形态，"
                "是否符合历史上ETL任务失败导致的数据故障特征？")

    hit = _mock_judgment(question, _hits("历史上出现过ETL任务失败导致分母缺失、比率虚高的情况"))
    assert hit["triggered"] is True

    miss = _mock_judgment(question, _hits("审批线变更需风控委员会审批并留存变更记录"))
    assert miss["triggered"] is False


def test_r5_approval_line_no_change_still_reads_hits_text():
    question = "根据知识库内容，审批线在最近三个月内是否【没有】发生过变更？（没有变更=triggered为true，作为对'审批口径变化'假设的反证）"

    no_change = _mock_judgment(question, _hits("当前审批线为580分，最近一次调整时间为2025年3月"))
    assert no_change["triggered"] is True

    changed = _mock_judgment(question, _hits("审批线于2026年7月18日下调至560分"))
    assert changed["triggered"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_llm_client.py -v`
Expected: `test_r6_seasonality_triggers_on_全渠道整体性_pattern` 的第一个断言 FAIL（现状永远返回 `False`）；`test_r8_data_quality_triggers_on_etl_pattern` 的第一个断言 FAIL；`test_r5_approval_line_no_change_still_reads_hits_text` 应该已经 PASS（r5 本来就是读内容判断的，这个测试是回归锁定，不是本次要修的）。

- [ ] **Step 3: 修复实现**

编辑 `agent/llm_client.py` 的 `_mock_judgment()`，把 r6 和 r8 两个分支从硬编码常量改成读 `hits_text`：

```python
def _mock_judgment(question: str, tool_result: dict) -> dict:
    hits_text = json.dumps(tool_result.get("hits", []), ensure_ascii=False)

    if "渠道放量" in question or "渠道" in question and "相似" in question:
        ok = "channel_quality_decline" in hits_text
        return {"triggered": ok, "confidence": 0.85 if ok else 0.2,
                "reasoning": "检索到渠道放量致逾期抬升的历史案例，情形高度相似" if ok else "未检索到相似渠道案例"}

    if "审批线" in question and "没有" in question:
        ok = "2025年3月" in hits_text
        return {"triggered": ok, "confidence": 0.9 if ok else 0.3,
                "reasoning": "KB显示审批线上次调整为2025年3月，近三个月无变更记录" if ok else "KB中未找到审批线变更信息"}

    if "季节" in question or "7月下旬" in question:
        ok = "全渠道" in hits_text and "整体" in hits_text
        return {"triggered": ok, "confidence": 0.75 if ok else 0.3,
                "reasoning": "检索到全渠道整体性抬升的历史案例，与当前时段模式匹配" if ok
                             else "未检索到全渠道整体性抬升的历史案例，或案例描述指向单一渠道异常"}

    if "ETL" in question or "数据故障" in question:
        ok = any(kw in hits_text for kw in ("ETL", "分母", "当日", "单日"))
        return {"triggered": ok, "confidence": 0.8 if ok else 0.2,
                "reasoning": "检索内容提示存在ETL/分母口径异常的历史记录" if ok else "未检索到历史数据故障特征相关记录"}

    return {"triggered": False, "confidence": 0.3, "reasoning": "mock模式未匹配到判断规则"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_llm_client.py -v`
Expected: 3 个测试全部 PASS。

- [ ] **Step 5: 跑一次全量回归**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest -q`
Expected: 全部通过，包括 `tests/test_smoke.py`（默认剧情的结论不应该因为这个修复而改变——默认剧情走的是 r1/r2/r3 数值+渠道案例证据，跟 r6/r8 无关）。

- [ ] **Step 6: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add agent/llm_client.py tests/test_llm_client.py
git commit -m "fix: _mock_judgment r6/r8 branches now read tool_result instead of hardcoded constants"
```

---

## Task 3: `data/scenarios.py` 骨架 + `channel_quality_decline` 场景生成器

**Files:**
- Create: `data/scenarios.py`
- Test: `tests/test_scenarios.py`（新增）

**Interfaces:**
- Consumes: `agent.scoring._channel_share_shift`、`agent.scoring._channel_overdue_gap`（只在测试里用，验证生成的数据真的触发规则）
- Produces:
  - `_draw_channel(n: int, rng: np.random.Generator, p=(0.55, 0.35, 0.10)) -> np.ndarray`
  - `_draw_score(n: int, rng: np.random.Generator, mu: float = 660, sigma: float = 45) -> np.ndarray`
  - `_base_p_overdue(score: np.ndarray) -> np.ndarray`
  - `_assemble_day(date: pd.Timestamp, rng: np.random.Generator, channel: np.ndarray, score: np.ndarray, cutoff: float, p_overdue: np.ndarray) -> pd.DataFrame`
  - `_days() -> list[pd.Timestamp]`
  - `_gen_channel_quality_decline(seed: int) -> pd.DataFrame`
  - 这些函数签名后续任务（4-7）会复用，不要改名字

- [ ] **Step 1: 写失败测试**

创建 `tests/test_scenarios.py`：

```python
"""data/scenarios.py 的量级校验：确认每类场景生成的数据真的会触发/不触发
对应的 agent/scoring.py 数值规则，而不是"看起来差不多但量级不够"。

用真实的 tools/dashboard.query_dashboard + agent/scoring 里的规则函数
端到端验证，不重新实现一遍判断逻辑。
"""
import pandas as pd

import config
import tools.dashboard as dashboard
from agent.scoring import _channel_share_shift, _channel_overdue_gap
from data.scenarios import _gen_channel_quality_decline

SPLIT = "2026-07-20"
FULL_RANGE = "2026-06-01~2026-07-30"
ANOMALY_RANGE = "2026-07-20~2026-07-30"


def _query(df: pd.DataFrame, tmp_path, monkeypatch, metric_name, date_range, dimensions=None):
    csv_path = tmp_path / "scenario.csv"
    df.to_csv(csv_path, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(csv_path))
    dashboard._load.cache_clear()
    try:
        return dashboard.query_dashboard(metric_name, date_range, dimensions)
    finally:
        dashboard._load.cache_clear()


def test_channel_quality_decline_triggers_share_and_overdue_rules(tmp_path, monkeypatch):
    df = _gen_channel_quality_decline(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert triggered

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    triggered, _ = _channel_overdue_gap(overdue, SPLIT)
    assert triggered
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'data.scenarios'`（文件还不存在）。

- [ ] **Step 3: 创建 `data/scenarios.py`**

```python
"""场景化 mock 数据生成器：credit-anomaly-agent 的评估用多场景数据源。

data/generate_mock_data.py 只产出一份写死的"渠道质量下滑"故事线。
本模块把它推广成参数化的多场景生成器，服务于 scripts/eval_scenarios.py：
每类假设各配一个真值场景（+ 2 个不同随机种子的变体），外加一个无结构性
信号的模糊负例，用于检验 agent 在证据不足时是否仍给出高置信度结论。

生成的 DataFrame schema 与 data/generate_mock_data.py 输出的 mock_loans.csv
完全一致（date/user_id/channel/city_tier/age_group/credit_score/approved/
loan_amount/fpd30），可以直接喂给 tools/dashboard.py。

不修改 data/generate_mock_data.py 本身——那是 README 快速开始命令依赖的
默认演示数据，保持独立。
"""
import numpy as np
import pandas as pd

START, END = "2026-06-01", "2026-07-30"
ANOMALY_START = pd.Timestamp("2026-07-20")
CHANNELS = ["channel_A", "channel_B", "channel_C"]
CITY_TIERS = ["T1", "T2", "T3"]
AGE_GROUPS = ["18-25", "26-35", "36-45", "46+"]


# ── 公共构件 ─────────────────────────────────────────
def _draw_channel(n: int, rng: np.random.Generator, p=(0.55, 0.35, 0.10)) -> np.ndarray:
    return rng.choice(CHANNELS, size=n, p=list(p))


def _draw_score(n: int, rng: np.random.Generator, mu: float = 660, sigma: float = 45) -> np.ndarray:
    return rng.normal(mu, sigma, size=n).clip(300, 850)


def _base_p_overdue(score: np.ndarray) -> np.ndarray:
    return np.clip(0.55 - score / 1600, 0.01, 0.5)


def _assemble_day(date: pd.Timestamp, rng: np.random.Generator, channel: np.ndarray,
                   score: np.ndarray, cutoff: float, p_overdue: np.ndarray) -> pd.DataFrame:
    """给定一天的渠道分配/信用分/审批线/逾期概率，装配成放款记录，只保留放款成功的。"""
    n = len(channel)
    approved = score >= cutoff
    is_overdue = rng.random(n) < p_overdue
    df = pd.DataFrame({
        "date": date.strftime("%Y-%m-%d"),
        "user_id": [f"u_{date.strftime('%m%d')}_{i:04d}" for i in range(n)],
        "channel": channel,
        "city_tier": rng.choice(CITY_TIERS, size=n, p=[0.3, 0.4, 0.3]),
        "age_group": rng.choice(AGE_GROUPS, size=n, p=[0.25, 0.4, 0.25, 0.1]),
        "credit_score": score.round(0),
        "approved": approved,
        "loan_amount": rng.choice([2000, 5000, 8000, 12000, 20000], size=n),
        "fpd30": is_overdue & approved,
    })
    return df[df["approved"]]


def _days() -> list[pd.Timestamp]:
    return list(pd.date_range(START, END, freq="D"))


# ── 场景生成器 ────────────────────────────────────────
def _gen_channel_quality_decline(seed: int) -> pd.DataFrame:
    """真值：channel_quality_decline。channel_C 异常期放量（10%→35%），
    该渠道用户信用分系统性偏低（-50分），额外携带+9pp逾期风险。
    跟 data/generate_mock_data.py 的默认故事线同一套参数，只是可换随机种子。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        p = (0.42, 0.23, 0.35) if is_anomaly else (0.55, 0.35, 0.10)
        channel = _draw_channel(n, rng, p)
        base = _draw_score(n, rng)
        score = np.where(channel == "channel_C", (base - 50).clip(300, 850), base)
        p_overdue = _base_p_overdue(score)
        p_overdue = np.where(channel == "channel_C", np.clip(p_overdue + 0.09, 0.01, 0.9), p_overdue)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: PASS。如果 `_channel_share_shift` 或 `_channel_overdue_gap` 没触发，说明随机噪声导致这次生成的数据量级不够——检查是不是 `p_overdue`/`score` 计算写错了（对照 `data/generate_mock_data.py` 里 `sample_day()` 的原始逻辑核对参数），而不是放宽阈值。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add scenarios.py skeleton with channel_quality_decline generator"
```

---

## Task 4: `approval_policy_change` 场景生成器

**Files:**
- Modify: `data/scenarios.py`（追加函数）
- Test: `tests/test_scenarios.py`（追加测试）

**Interfaces:**
- Consumes: Task 3 的 `_draw_channel`、`_draw_score`、`_base_p_overdue`、`_assemble_day`、`_days`
- Produces: `_gen_approval_policy_change(seed: int) -> pd.DataFrame`

**关键设计决策**：这个场景不通过"审批线数值下调"来制造信号，而是直接让异常期的信用分生成分布整体下移（mu 从 660 降到 630，sigma 不变），审批线（cutoff）全程保持 580 不变。

原因（已用截断正态分布算过）：当前 `score ~ N(660, 45)`、`cutoff=580` 时，`z = (580-660)/45 ≈ -1.78`，已经只截掉约 4% 的最低分人群。单纯下调 cutoff 数值（哪怕降到 400）能拉低的"已放款人群平均分"上限只有约 3-4 分——因为能补充进来的低分人群本身占比太小，不可能达到 r4 要求的 >10 分降幅。所以改用"直接下移整体信用分生成分布"来模拟"审批口径事实上放宽"，这是本次 mock 数据的简化处理，不代表真实业务里"降 cutoff 一定测不出信号"。

用同样的截断正态公式验算：`mu=630` 时 `E[X|X>=580] ≈ 641.2`，`mu=660` 时 `E[X|X>=580] ≈ 663.9`，降幅约 22.7 分，比 10 分阈值有充分余量（覆盖 3 个随机种子的抽样噪声）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scenarios.py` 追加（更新 import 行加入新函数和 `_score_drop`）：

```python
from agent.scoring import _channel_share_shift, _channel_overdue_gap, _score_drop
from data.scenarios import _gen_channel_quality_decline, _gen_approval_policy_change


def test_approval_policy_change_triggers_score_drop_but_not_channel_rules(tmp_path, monkeypatch):
    df = _gen_approval_policy_change(seed=42)

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    triggered, _ = _score_drop(score, SPLIT)
    assert triggered

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert not triggered
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py::test_approval_policy_change_triggers_score_drop_but_not_channel_rules -v`
Expected: FAIL，`ImportError`（`_gen_approval_policy_change` 还不存在）。

- [ ] **Step 3: 在 `data/scenarios.py` 追加生成器**

紧跟在 `_gen_channel_quality_decline` 后面：

```python
def _gen_approval_policy_change(seed: int) -> pd.DataFrame:
    """真值：approval_policy_change。渠道结构、渠道级逾期风险均不变；
    异常期整体准入信用分分布下移30分（模拟审批口径事实上放宽），
    审批线（cutoff）数值本身保持580不变——纯 cutoff 下调在当前分数分布下
    不足以让均分降超过10分（截断正态分布的算术上限约3-4分），
    所以用分布整体下移来制造可测的信号，这是本次 mock 的简化处理。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        channel = _draw_channel(n, rng)
        mu = 630 if is_anomaly else 660
        score = _draw_score(n, rng, mu=mu)
        p_overdue = _base_p_overdue(score)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add approval_policy_change scenario generator"
```

---

## Task 5: `macro_seasonality` 场景生成器

**Files:**
- Modify: `data/scenarios.py`（追加函数）
- Test: `tests/test_scenarios.py`（追加测试）

**Interfaces:**
- Consumes: Task 3 的公共构件
- Produces: `_gen_macro_seasonality(seed: int) -> pd.DataFrame`

**说明**：`macro_seasonality` 假设在 `agent/hypotheses.py` 里只有一条 llm_judgment 规则（r6），没有对应的数值规则——所以这个场景的验证方式跟其他场景不同：不是"触发某个数值规则"，而是"确实存在全渠道同步的逾期抬升信号，同时不会误触发 r1/r2/r4 这些数值规则"。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scenarios.py` 追加：

```python
from data.scenarios import (
    _gen_channel_quality_decline, _gen_approval_policy_change, _gen_macro_seasonality,
)


def test_macro_seasonality_raises_overdue_without_channel_or_score_signal(tmp_path, monkeypatch):
    df = _gen_macro_seasonality(seed=42)

    baseline_rate = df[df["date"] < SPLIT]["fpd30"].mean()
    anomaly_rate = df[df["date"] >= SPLIT]["fpd30"].mean()
    assert anomaly_rate - baseline_rate > 0.03  # 确实存在整体抬升信号，不是纯噪声

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    assert not _channel_share_shift(share, SPLIT)[0]

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    assert not _score_drop(score, SPLIT)[0]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py::test_macro_seasonality_raises_overdue_without_channel_or_score_signal -v`
Expected: FAIL，`ImportError`。

- [ ] **Step 3: 在 `data/scenarios.py` 追加生成器**

```python
def _gen_macro_seasonality(seed: int) -> pd.DataFrame:
    """真值：macro_seasonality。渠道结构、信用分分布全程不变；
    异常期所有渠道逾期概率同步+5pp（无渠道级差异，用来跟
    channel_quality_decline 的渠道特异性风险区分开）。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        if is_anomaly:
            p_overdue = np.clip(p_overdue + 0.05, 0.01, 0.9)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add macro_seasonality scenario generator"
```

---

## Task 6: `data_quality_issue` 场景生成器

**Files:**
- Modify: `data/scenarios.py`（追加函数）
- Test: `tests/test_scenarios.py`（追加测试）

**Interfaces:**
- Consumes: Task 3 的公共构件
- Produces: `_gen_data_quality_issue(seed: int) -> pd.DataFrame`

- [ ] **Step 1: 写失败测试**

在 `tests/test_scenarios.py` 追加：

```python
from agent.scoring import (
    _channel_share_shift, _channel_overdue_gap, _score_drop, _count_jump,
)
from data.scenarios import (
    _gen_channel_quality_decline, _gen_approval_policy_change,
    _gen_macro_seasonality, _gen_data_quality_issue,
)


def test_data_quality_issue_triggers_count_jump(tmp_path, monkeypatch):
    df = _gen_data_quality_issue(seed=42)
    count = _query(df, tmp_path, monkeypatch, "loan_count", ANOMALY_RANGE)
    triggered, _ = _count_jump(count, SPLIT)
    assert triggered
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py::test_data_quality_issue_triggers_count_jump -v`
Expected: FAIL，`ImportError`。

- [ ] **Step 3: 在 `data/scenarios.py` 追加生成器**

```python
def _gen_data_quality_issue(seed: int) -> pd.DataFrame:
    """真值：data_quality_issue。真实逾期率/渠道结构/信用分全程平稳；
    异常期正中间那天模拟ETL任务失败，60%的放款记录丢失（分母塌陷），
    制造单日放款笔数跳变，同时不影响其他天的真实指标。"""
    rng = np.random.default_rng(seed)
    days = _days()
    anomaly_days = [d for d in days if d >= ANOMALY_START]
    glitch_day = anomaly_days[len(anomaly_days) // 2]
    frames = []
    for date in days:
        n = int(rng.normal(400, 30))
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        day_df = _assemble_day(date, rng, channel, score, 580, p_overdue)
        if date == glitch_day:
            keep_mask = rng.random(len(day_df)) < 0.4
            day_df = day_df[keep_mask]
        frames.append(day_df)
    return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: 全部 PASS。如果 `_count_jump` 没触发，把 `keep_mask` 的保留比例从 `0.4` 调低（如 `0.3`）加大偏离幅度，不要去改 `agent/scoring.py` 里 `count_jump` 的 40% 阈值——阈值是被测对象，不是要迁就的东西。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add data_quality_issue scenario generator"
```

---

## Task 7: `ambiguous_negative` 场景生成器 + `SCENARIOS` 注册表 + `iter_runs()`

**Files:**
- Modify: `data/scenarios.py`（追加函数 + 注册表）
- Test: `tests/test_scenarios.py`（追加测试）

**Interfaces:**
- Consumes: Task 3-6 的所有生成器函数
- Produces:
  - `_gen_ambiguous_negative(seed: int) -> pd.DataFrame`
  - `ScenarioSpec`（dataclass：`scenario_type: str`, `ground_truth: str | None`, `phenomenon: str`, `generator: Callable[[int], pd.DataFrame]`, `seeds: tuple[int, ...]`）
  - `SCENARIOS: dict[str, ScenarioSpec]`
  - `iter_runs() -> Iterator[tuple[str, pd.DataFrame, ScenarioSpec]]`——这是 Task 8 `scripts/eval_scenarios.py` 唯一依赖的入口，`run_id` 格式必须是 `f"{scenario_type}_s{seed}"`

- [ ] **Step 1: 写失败测试**

在 `tests/test_scenarios.py` 追加（补全 import）：

```python
from data.scenarios import (
    SCENARIOS, iter_runs,
    _gen_channel_quality_decline, _gen_approval_policy_change,
    _gen_macro_seasonality, _gen_data_quality_issue, _gen_ambiguous_negative,
)


def test_ambiguous_negative_triggers_no_numeric_rule(tmp_path, monkeypatch):
    df = _gen_ambiguous_negative(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    assert not _channel_share_shift(share, SPLIT)[0]

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    assert not _channel_overdue_gap(overdue, SPLIT)[0]

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    assert not _score_drop(score, SPLIT)[0]

    count = _query(df, tmp_path, monkeypatch, "loan_count", ANOMALY_RANGE)
    assert not _count_jump(count, SPLIT)[0]


def test_scenarios_registry_covers_all_hypotheses_and_seeds():
    assert set(SCENARIOS) == {
        "channel_quality_decline", "approval_policy_change",
        "macro_seasonality", "data_quality_issue", "ambiguous_negative",
    }
    for scenario_type, spec in SCENARIOS.items():
        assert spec.scenario_type == scenario_type

    run_ids = [run_id for run_id, _df, _spec in iter_runs()]
    assert len(run_ids) == len(set(run_ids)) == 13
    assert "channel_quality_decline_s42" in run_ids
    assert SCENARIOS["ambiguous_negative"].ground_truth is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -k "ambiguous_negative or registry" -v`
Expected: FAIL，`ImportError`（`_gen_ambiguous_negative`、`SCENARIOS`、`iter_runs` 都还不存在）。

- [ ] **Step 3: 在 `data/scenarios.py` 追加生成器 + 注册表**

先加 import（文件顶部，在现有 `import numpy as np` / `import pandas as pd` 之后）：

```python
from dataclasses import dataclass
from typing import Callable, Iterator
```

在 `_gen_data_quality_issue` 后面追加：

```python
def _gen_ambiguous_negative(seed: int) -> pd.DataFrame:
    """无真值：全程使用同一套稳态参数，异常期和基线期只有随机噪声差异，
    没有任何结构性变化——用于检验 agent 在证据不足时是否仍给出高置信度结论。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


# ── 场景注册表 ────────────────────────────────────────
@dataclass(frozen=True)
class ScenarioSpec:
    scenario_type: str
    ground_truth: str | None
    phenomenon: str
    generator: Callable[[int], pd.DataFrame]
    seeds: tuple[int, ...]


SCENARIOS: dict[str, ScenarioSpec] = {
    "channel_quality_decline": ScenarioSpec(
        "channel_quality_decline", "channel_quality_decline",
        "新增用户FPD30逾期率近期环比明显上升",
        _gen_channel_quality_decline, (42, 101, 202)),
    "approval_policy_change": ScenarioSpec(
        "approval_policy_change", "approval_policy_change",
        "新增用户FPD30逾期率近期缓慢爬升，信用分分布疑似左移",
        _gen_approval_policy_change, (42, 101, 202)),
    "macro_seasonality": ScenarioSpec(
        "macro_seasonality", "macro_seasonality",
        "全渠道FPD30逾期率近期同步小幅上升，无单一渠道异常",
        _gen_macro_seasonality, (42, 101, 202)),
    "data_quality_issue": ScenarioSpec(
        "data_quality_issue", "data_quality_issue",
        "FPD30逾期率异常期内某日出现数值跳变",
        _gen_data_quality_issue, (42, 101, 202)),
    "ambiguous_negative": ScenarioSpec(
        "ambiguous_negative", None,
        "新增用户FPD30逾期率近期波动，暂无明确异常信号",
        _gen_ambiguous_negative, (42,)),
}


def iter_runs() -> Iterator[tuple[str, pd.DataFrame, ScenarioSpec]]:
    """产出 (run_id, dataframe, spec)，run_id 形如 channel_quality_decline_s42。"""
    for spec in SCENARIOS.values():
        for seed in spec.seeds:
            run_id = f"{spec.scenario_type}_s{seed}"
            yield run_id, spec.generator(seed), spec
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. pytest tests/test_scenarios.py -v`
Expected: 全部 PASS（这时候 `tests/test_scenarios.py` 应该有 6 个测试）。

- [ ] **Step 5: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add ambiguous_negative scenario + SCENARIOS registry + iter_runs()"
```

---

## Task 8: `scripts/eval_scenarios.py` 批量 eval 脚本

**Files:**
- Create: `scripts/eval_scenarios.py`
- Modify: `.gitignore`（追加 `eval_results/`）

**Interfaces:**
- Consumes: `data.scenarios.SCENARIOS`、`data.scenarios.iter_runs()`（Task 7）、`agent.orchestrator.Orchestrator`、`tools.dashboard._load`（`functools.lru_cache` 装饰的函数，需要 `.cache_clear()`）、`config.DATA_PATH`
- Produces: CLI 脚本，`python scripts/eval_scenarios.py [--scenario TYPE] [--out DIR]`；终端打印汇总表 + 写 `eval_results/<timestamp>.json`

这个脚本是手动运行的调优工具，不写单元测试（跟 spec 里定的一致），但本任务最后一步要求实际跑一次全部 13 个场景，确认脚本本身能正常工作。

- [ ] **Step 1: 创建 `scripts/eval_scenarios.py`**

```python
"""批量跑 Orchestrator 对比多场景真值，产出准确率/置信度/收敛情况汇总。
手动运行的调优工具，不接入 CI。

用法：
    cd credit-anomaly-agent
    PYTHONPATH=. python scripts/eval_scenarios.py                              # 跑全部13个场景
    PYTHONPATH=. python scripts/eval_scenarios.py --scenario macro_seasonality # 只跑某一类
    PYTHONPATH=. python scripts/eval_scenarios.py --out eval_results          # 指定输出目录（默认值）
"""
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


def run_one(run_id: str, df, spec) -> dict:
    """写临时 CSV -> 替换 config.DATA_PATH -> 清 dashboard 缓存 -> 跑 Orchestrator -> 对比真值。

    config.DATA_PATH 是全局单例，tools/dashboard.py 的 _load() 又用
    @lru_cache(maxsize=1) 无参缓存——不清缓存的话，第二个场景开始就会一直
    读第一个场景的数据。用 try/finally 保证即使某个场景跑挂了，
    也会把 config.DATA_PATH 恢复原值，不污染后续手动调试。
    """
    tmp_dir = tempfile.mkdtemp(prefix="eval_scenario_")
    tmp_csv = os.path.join(tmp_dir, f"{run_id}.csv")
    df.to_csv(tmp_csv, index=False)
    original_path = config.DATA_PATH
    try:
        config.DATA_PATH = tmp_csv
        dashboard._load.cache_clear()
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
    finally:
        config.DATA_PATH = original_path
        dashboard._load.cache_clear()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _print_table(results: list[dict]) -> None:
    header = f"{'场景':<32}{'真值':<26}{'预测':<26}{'命中':<6}{'置信度':<8}{'次优分差':<10}{'工具调用':<8}"
    print(header)
    print("-" * len(header))
    scored = [r for r in results if r.get("ground_truth")]
    hits = 0
    for r in results:
        if "error" in r:
            print(f"{r['run_id']:<32}{'ERROR':<26}{r['error'][:60]}")
            continue
        hit_mark = "n/a" if r["hit"] is None else ("✓" if r["hit"] else "✗")
        if r["hit"]:
            hits += 1
        print(f"{r['run_id']:<32}{str(r['ground_truth']):<26}{r['predicted']:<26}"
              f"{hit_mark:<6}{r['confidence']:<8.3f}{r['runner_up_gap']:<10.3f}{r['tool_calls_used']:<8}")
    if scored:
        print(f"\n准确率: {hits}/{len(scored)} ({hits / len(scored):.1%})")


def main(scenario_filter: str | None, out_dir: str) -> list[dict]:
    results = []
    for run_id, df, spec in iter_runs():
        if scenario_filter and spec.scenario_type != scenario_filter:
            continue
        results.append(run_one(run_id, df, spec))

    _print_table(results)

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(out_dir, f"{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果已写入 {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=None,
                         help="只跑指定类型（不传则跑全部场景）")
    parser.add_argument("--out", default="eval_results", help="JSON 结果输出目录")
    args = parser.parse_args()
    main(args.scenario, args.out)
```

- [ ] **Step 2: 追加 `.gitignore` 排除生成的结果目录**

编辑 `.gitignore`，在末尾追加一行（跟现有 `data/mock_loans.csv` 一样，生成文件不进 git）：

```
eval_results/
```

- [ ] **Step 3: 跑全部 13 个场景，检查脚本本身能正常工作**

Run: `cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent" && PYTHONPATH=. python scripts/eval_scenarios.py`

Expected:
- 终端打印一张 13 行的表格，`ERROR` 行数应该是 0（如果某场景报错，说明前面任务的生成器有 bug，回去对应任务修，不要在 eval 脚本里加 try/except 掩盖）。
- 打印"准确率: N/12"（`ambiguous_negative` 没有真值，不计入分母，分母应为 12）。
- 结尾打印一行"完整结果已写入 eval_results/xxxxx.json"，检查该文件存在且是合法 JSON（`python -c "import json; json.load(open('eval_results/<文件名>.json'))"` 不报错）。
- 运行结束后确认 `config.py` 里 `DATA_PATH` 打印出来还是默认值（`python -c "import config; print(config.DATA_PATH)"` 应该输出以 `mock_loans.csv` 结尾的路径，不是临时目录路径）——验证 `finally` 里的恢复逻辑生效。

这一步的目的不是要求 13 个场景全部预测命中（那是下一步优化迭代要做的事），而是确认整条链路——生成数据、灌进 dashboard、跑 orchestrator、算命中率、落盘——本身没有 bug。如果准确率明显偏低（比如 macro_seasonality 系列全错），先看 `ranking` 里的 `evidence_log`，确认是数据量级问题还是规则本身的问题，再决定要不要回头调 Task 4-6 的场景参数。

- [ ] **Step 4: Commit**

```bash
cd "/Users/yaobrinda/Desktop/Agent building/credit-anomaly-agent"
git add scripts/eval_scenarios.py .gitignore
git commit -m "feat: add scripts/eval_scenarios.py batch eval harness"
```

---

## 完成后的验收清单

- [ ] `PYTHONPATH=. pytest -q` 全部通过（原有测试 + 本次新增的约 13 个测试）
- [ ] `PYTHONPATH=. python scripts/eval_scenarios.py` 跑完 13 个场景，0 个 `ERROR` 行
- [ ] `eval_results/` 目录已加入 `.gitignore`，`git status` 干净
- [ ] `data/generate_mock_data.py`、`tools/dashboard.py`、`agent/orchestrator.py`、`agent/hypotheses.py`、`agent/scoring.py`、`kb/*.json` 均未被修改（`git diff main~8 -- data/generate_mock_data.py tools/dashboard.py agent/orchestrator.py agent/hypotheses.py agent/scoring.py kb/` 应为空，具体对比哪个 commit 视实际提交数调整）
