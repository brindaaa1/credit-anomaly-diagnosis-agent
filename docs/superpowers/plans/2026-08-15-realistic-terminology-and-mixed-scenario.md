# 风控黑话落地 + 多因交织场景 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把真实风控黑话落进业务知识库和案例文案，并新增一个信号刻意调弱的"渠道+监管"多因交织场景，验证置信度地板/reflect 机制在证据模糊时不会过度自信。

**Architecture:** 四个独立任务：① 业务知识库新增术语词条 ② 4 条种子案例文案润色 ③ 新场景生成器 + 信号强度断言 + 场景注册表同步 ④ 多因场景打分层测试（直接调 `run_probe`+`apply_rules`，不跑完整 `Orchestrator.run()`——`regulatory_policy_change` 排在假设字典最后，正常搜索预算会在轮到它之前耗尽）。

**Tech Stack:** Python 3.12, pandas, pytest（现有依赖，不新增任何包）。

## Global Constraints

- 不改任何 CSV schema 字段名（`channel`/`credit_score` 等英文列名不变），黑话只出现在中文文案层。
- 不改 `agent/hypotheses.py`、`agent/orchestrator.py`、`agent/scoring.py`、`data/scenario_runtime.py`、`scripts/eval_scenarios.py`——这次改动完全复用这些文件现有的能力。
- 不新增第 6 个假设类型，不改 `agent/llm_client.py::_mock_judgment` 的关键词匹配逻辑。
- 新场景 `policy_and_channel_mixed` 的 `ground_truth` 固定为 `"channel_quality_decline"`，三个随机种子变体为 `(42, 101, 202)`，跟现有场景注册惯例一致。
- `tests/test_multi_factor_scenario.py` 里判断"两个假设分数接近"的阈值：`abs(channel_score - regulatory_score) < 0.25`（实现时如与实测值不符，以实测值为准调整，但需在提交信息里写明改了什么、为什么）。
- 每个任务完成后运行 `PYTHONPATH=. python -m pytest tests/ -v` 确保全量测试仍然通过（当前基线：64 passed，运行前需已执行过 `python data/generate_mock_data.py` 生成 `data/mock_loans.csv`，该文件已在 `.gitignore` 中）。

---

### Task 1: `kb/business_kb.md` 新增黑话词条

**Files:**
- Modify: `kb/business_kb.md:17-19`（在"## 监管政策"词条之后、"## Vintage报表(3)..."之前插入）
- Test: `tests/test_retrieval.py`

**Interfaces:**
- 不产出新的 Python 接口，只是给 `tools/retrieval.py::retrieve_business_kb()` 已有的检索能力增加可命中的词条。

- [ ] **Step 1: 写失败测试**

在 `tests/test_retrieval.py` 末尾追加：

```python
def test_business_kb_includes_new_risk_terms():
    from tools.retrieval import _load_kb_md
    import config

    entries = _load_kb_md(config.KB_PATH)
    terms = {e["term"] for e in entries}
    for expected in ["1m30 / 1m30_amount", "多头", "找黑能力", "睡眠用户", "戳额", "标的 / 发标", "新客额度 / 老客额度", "期数金额加权"]:
        assert expected in terms, f"缺少词条: {expected}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. python -m pytest tests/test_retrieval.py -v`
Expected: FAIL，`AssertionError: 缺少词条: ...`

- [ ] **Step 3: 在 `kb/business_kb.md` 里插入新词条**

在第 17 行（"## 监管政策"词条的正文那一行）之后、第 19 行（"## Vintage报表(3)：Python代码实操"）之前插入（保留原有的空行分隔）：

```markdown

## 1m30 / 1m30_amount
逾期指标通用命名：1m30指第1期30天逾期，按期数可扩展为2m30/3m30等，衡量特定期数是否发生30天以上逾期。本demo用FPD30（即1m30，笔数口径）作为具体实现；1m30_amount是同一类指标的金额口径（逾期金额/应还金额），比笔数口径更能反映真实资金风险敞口，实际业务中两个口径会分开监控

## 多头
多头借贷：借款人在同一时间于多家网贷/助贷平台存在未结清负债。多头数量越高，通常意味着借款人真实杠杆和还款压力被低估，是获客渠道质量评估的重要辅助维度，可与渠道占比、信用分分布交叉验证

## 找黑能力
风控模型/策略对高风险（"黑"）用户的区分能力，侧重能否把尾部坏客户挑出来，跟AUC/KS这类整体区分度指标视角不同——找黑能力弱时，即使整体指标正常，某个渠道或客群里少数高风险用户也可能被漏过，导致局部逾期抬升

## 睡眠用户
已完成授信但一段时间内没有提款/借款行为的用户，是评估存量客群活跃度和转化潜力的维度，跟FPD30这类新增资产指标无直接关系，但会影响戳额、复借这类行为信号的解读

## 戳额
用户在App内主动点开查看自己的授信额度，是一种借款意愿信号；戳额频次上升有时领先于实际借款行为一段时间，可作为需求端变化的早期观察指标

## 标的 / 发标
标的：借款对应的资产/合同标的物；发标：平台把借款需求挂出、匹配资金方的过程。两者是网贷/助贷行业对"一笔借款从申请到匹配资金"这个环节的常用说法

## 新客额度 / 老客额度
授信额度按客群分开管理：新客额度对应首次借款用户，老客额度对应有借款历史的复借用户，两者的审批策略、额度策略通常独立调整，排查审批口径类问题时要区分是哪一类客群的额度策略变了

## 期数金额加权
按分期数对放款金额做加权统计的口径：同样一笔本金，分期数越多，利息/手续费收入通常越高，期数金额加权更能反映业务的实际盈利贡献，不能只看放款规模（笔数×金额）
```

（注意：`_load_kb_md()` 用 `re.split(r"\n(?=## )", text.strip())` 按 `## ` 前缀切块，所以每个词条必须以 `## ` 开头独占一行，词条之间保留一个空行，格式跟文件里现有词条完全一致即可，不需要额外处理）

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_retrieval.py -v`
Expected: PASS（3 passed：原有 2 个 + 新增 1 个）

- [ ] **Step 5: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（65 passed）

- [ ] **Step 6: 提交**

```bash
git add kb/business_kb.md tests/test_retrieval.py
git commit -m "docs: add risk-control jargon glossary entries to business KB"
```

---

### Task 2: `kb/historical_cases.json` 种子案例文案润色

**Files:**
- Modify: `kb/historical_cases.json`（4 条原始种子案例：`case_2025_11`/`case_2025_08`/`case_2026_02`/`case_2025_05`）

**Interfaces:**
- 不改 JSON 结构（`case_id`/`phenomenon`/`investigation`/`conclusion`/`root_cause_tag` 字段名和每条记录的 `root_cause_tag` 值都不变），只改文案内容。

**⚠️ 风险点（跟原 spec 有一处刻意偏离，读完再动手）：** `tests/test_curation.py:107` 的 `test_novelty_low_for_near_duplicate_of_seed_case` 硬编码了 `case_2025_11` 当前的 `phenomenon` 原文 `"新增用户FPD30逾期率单周环比上升25%，集中在某新接入渠道"`，用来验证"近似重复案例应该拿到低新颖性分"。如果把这条案例的 `FPD30` 也换成 `1m30`，会削弱这条测试用的查询文本和案例正文之间的 TF-IDF 相似度，有实际跑挂的风险。所以下面 `case_2025_11` 的改法**保留 `FPD30` 不动**，只在末尾追加一句提到"多头"的内容——跟原 spec 文档里写的版本不完全一致，这是为了不冒这个回归风险，是设计阶段没预料到、写 plan 时才发现的细节调整。

- [ ] **Step 1: 编辑 `case_2025_11`（渠道质量下滑）**

把：
```json
    "phenomenon": "新增用户FPD30逾期率单周环比上升25%，集中在某新接入渠道",
    "investigation": "按渠道拆解逾期率 -> 发现渠道X占比从8%升到30% -> 对比该渠道用户信用分分布，均值低于大盘35分",
    "conclusion": "渠道质量下滑：新渠道放量且用户资质偏弱，建议对该渠道单独提高审批线",
```
改成：
```json
    "phenomenon": "新增用户FPD30逾期率单周环比上升25%，集中在某新接入渠道，且该渠道新客多头占比偏高",
    "investigation": "按渠道拆解逾期率 -> 发现渠道X占比从8%升到30% -> 对比该渠道用户信用分分布，均值低于大盘35分 -> 叠加多头借贷数据后发现该渠道新客多头占比显著高于大盘",
    "conclusion": "渠道质量下滑：新渠道放量且用户资质偏弱（多头占比高），建议对该渠道新客额度单独收紧",
```

- [ ] **Step 2: 编辑 `case_2025_08`（宏观季节性）**

把：
```json
    "investigation": "对比历史三年同期 -> 每年春节后均有类似抬升，幅度一致 -> 渠道结构和用户画像无变化",
```
改成：
```json
    "investigation": "对比历史三年同期 -> 每年春节后均有类似抬升，幅度一致 -> 渠道结构和用户画像无变化 -> 同期用户戳额（查看额度）频次同步上升，佐证是集中的短期还款压力而非渠道结构问题",
```
（`phenomenon`/`conclusion` 不改）

- [ ] **Step 3: 编辑 `case_2026_02`（数据质量问题）**

把：
```json
    "phenomenon": "某日逾期率数值跳变翻倍，次日恢复正常",
```
改成：
```json
    "phenomenon": "某日1m30逾期率数值跳变翻倍，次日恢复正常",
```
（这条没有被任何现有测试硬编码引用过，改 FPD30→1m30 安全）

- [ ] **Step 4: 编辑 `case_2025_05`（审批口径变化）**

把：
```json
    "phenomenon": "逾期率缓慢爬升一个月，新增用户信用分分布明显左移",
    "investigation": "检查审批策略变更记录 -> 发现风控为冲量下调审批线20分 -> 低分段用户占比上升",
    "conclusion": "审批口径变化：审批线下调引入更高风险用户，属预期内风险抬升",
```
改成：
```json
    "phenomenon": "逾期率缓慢爬升一个月，新客额度对应用户群的信用分分布明显左移",
    "investigation": "检查审批策略变更记录 -> 发现风控为冲量下调新客额度对应审批线20分 -> 低分段用户占比上升",
    "conclusion": "审批口径变化：新客额度审批线下调引入更高风险用户，属预期内风险抬升",
```

- [ ] **Step 5: 校验 JSON 格式合法**

Run: `python -c "import json; json.load(open('kb/historical_cases.json', encoding='utf-8')); print('OK')"`
Expected: 输出 `OK`，不报错（逗号/引号没有打错）

- [ ] **Step 6: 跑全量测试，重点关注 `test_novelty_low_for_near_duplicate_of_seed_case`**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（65 passed，延续 Task 1 的计数）。如果 `tests/test_curation.py::test_novelty_low_for_near_duplicate_of_seed_case` 失败（`_novelty(phenomenon) < 0.3` 断言不成立），说明即使保留了 `FPD30` 不变，追加的"多头"那句话依然把相似度拉得够低——处理办法：把 `tests/test_curation.py:107` 的查询字符串同步改成 `"新增用户FPD30逾期率单周环比上升25%，集中在某新接入渠道，且该渠道新客多头占比偏高"`（即改成新的 `case_2025_11` phenomenon 原文，保持"近似重复"这个测试意图不变），再跑一次确认通过。把实际发生了哪种情况写进最终报告。

- [ ] **Step 7: 提交**

```bash
git add kb/historical_cases.json
# 如果 Step 6 里改了 test_curation.py，一并加进来：
# git add tests/test_curation.py
git commit -m "docs: enrich seed case narratives with real risk-control terminology"
```

---

### Task 3: `data/scenarios.py` 新增多因交织场景生成器

**Files:**
- Modify: `data/scenarios.py`（新增 `_gen_policy_and_channel_mixed()`，注册进 `SCENARIOS`）
- Modify: `tests/test_scenarios.py`（新增信号强度断言 + 修复因新增场景而需要更新的注册表计数测试）

**Interfaces:**
- Produces: `data.scenarios._gen_policy_and_channel_mixed(seed: int) -> pandas.DataFrame`，schema 跟其余生成器一致（`date`/`user_id`/`channel`/`city_tier`/`age_group`/`credit_score`/`approved`/`loan_amount`/`fpd30`）。`SCENARIOS["policy_and_channel_mixed"]` 是新的 `ScenarioSpec` 条目，`ground_truth="channel_quality_decline"`。供 Task 4 使用。

**⚠️ 需要顺带修的地方（原 spec 没提到，写 plan 时发现的必然后果）：** `tests/test_scenarios.py` 末尾的 `test_scenarios_registry_covers_all_hypotheses_and_seeds()` 硬编码断言 `set(SCENARIOS)` 等于当前 5 个场景类型的集合、`len(run_ids) == 13`。注册新场景后这两个断言必然失败，必须在同一个任务里一起改，否则测试会红。

- [ ] **Step 1: 写失败测试**

在 `tests/test_scenarios.py` 顶部的 import 里，把：
```python
from data.scenarios import (
    SCENARIOS, iter_runs,
    _gen_channel_quality_decline, _gen_approval_policy_change,
    _gen_macro_seasonality, _gen_data_quality_issue, _gen_ambiguous_negative,
)
```
改成：
```python
from data.scenarios import (
    SCENARIOS, iter_runs,
    _gen_channel_quality_decline, _gen_approval_policy_change,
    _gen_macro_seasonality, _gen_data_quality_issue, _gen_ambiguous_negative,
    _gen_policy_and_channel_mixed,
)
```

在文件末尾（`test_scenarios_registry_covers_all_hypotheses_and_seeds` 之前）追加：

```python
def test_policy_and_channel_mixed_triggers_share_shift_but_not_overdue_gap(tmp_path, monkeypatch):
    """信号刻意调弱：r1（占比变化>15pp）应该触发，r2（渠道逾期率高出均值50%）
    大概率不应该触发——跟 test_channel_quality_decline_triggers_share_and_overdue_rules
    对比着看，同一个假设方向，但证据强度不同。"""
    df = _gen_policy_and_channel_mixed(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert triggered

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    triggered, _ = _channel_overdue_gap(overdue, SPLIT)
    assert not triggered
```

把现有的 `test_scenarios_registry_covers_all_hypotheses_and_seeds()`：
```python
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
改成：
```python
def test_scenarios_registry_covers_all_hypotheses_and_seeds():
    assert set(SCENARIOS) == {
        "channel_quality_decline", "approval_policy_change",
        "macro_seasonality", "data_quality_issue", "ambiguous_negative",
        "policy_and_channel_mixed",
    }
    for scenario_type, spec in SCENARIOS.items():
        assert spec.scenario_type == scenario_type

    run_ids = [run_id for run_id, _df, _spec in iter_runs()]
    assert len(run_ids) == len(set(run_ids)) == 16
    assert "channel_quality_decline_s42" in run_ids
    assert SCENARIOS["ambiguous_negative"].ground_truth is None
    assert SCENARIOS["policy_and_channel_mixed"].ground_truth == "channel_quality_decline"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. python -m pytest tests/test_scenarios.py -v`
Expected: FAIL，`ImportError: cannot import name '_gen_policy_and_channel_mixed'`

- [ ] **Step 3: 在 `data/scenarios.py` 里实现新生成器**

紧跟在 `_gen_channel_quality_decline` 函数定义之后插入：

```python
def _gen_policy_and_channel_mixed(seed: int) -> pd.DataFrame:
    """真值（主要）：channel_quality_decline，但信号调弱到接近规则触发边界；
    同时该场景配合专项测试（tests/test_multi_factor_scenario.py）时会叠加一份
    "监管全面收紧"的 mock 检索内容（regulatory_policy_change 的证据来源不在这份
    DataFrame 里，是 web_search 的 mock 内容），模拟"渠道结构变化 + 监管政策收紧"
    同期发生、互相干扰排查的真实场景。
    channel_C 占比涨幅（10%→26%，+16pp，刚过 r1 的 15pp 阈值）、信用分降幅
    （-20分，明显弱于单因场景的-50分）、逾期风险加成（+4pp，弱于单因场景的+9pp，
    大概率不足以触发 r2 的"高出均值50%"阈值）都刻意调弱，让 channel_quality_decline
    只能拿到 r1 的部分证据，不是 r1+r2 同时确凿命中。
    跑常规批量评估（scripts/eval_scenarios.py）时只用得到这份数据本身，
    web_search 走默认 mock 内容（不含"全面收紧"），所以在那条路径下这个场景
    体现的是"channel_quality_decline 信号变弱、置信度降低"，不是完整的双假设模糊——
    完整的模糊场景需要专项测试额外 monkeypatch MOCK_WEB_RESULTS_PATH 才能看到，
    详见 tests/test_multi_factor_scenario.py 顶部说明。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        p = (0.50, 0.24, 0.26) if is_anomaly else (0.55, 0.35, 0.10)
        channel = _draw_channel(n, rng, p)
        base = _draw_score(n, rng)
        score = np.where(channel == "channel_C", (base - 20).clip(300, 850), base)
        p_overdue = _base_p_overdue(score)
        p_overdue = np.where(channel == "channel_C", np.clip(p_overdue + 0.04, 0.01, 0.9), p_overdue)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)
```

在 `SCENARIOS` 字典里，紧跟在 `"channel_quality_decline"` 条目之后加一条：

```python
    "policy_and_channel_mixed": ScenarioSpec(
        "policy_and_channel_mixed", "channel_quality_decline",
        "新增用户1m30逾期率近期上升，渠道结构和监管环境同期都有变化，信号强度弱于典型场景",
        _gen_policy_and_channel_mixed, (42, 101, 202)),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. python -m pytest tests/test_scenarios.py -v`
Expected: PASS（7 passed：原有 6 个 + 新增 1 个信号强度测试；注册表测试是修改断言，不是新增函数，不增加计数）

- [ ] **Step 5: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（66 passed，延续 Task 1/2 的计数）

- [ ] **Step 6: 手动跑一遍批量评估，确认新场景能正常产出结果**

Run: `PYTHONPATH=. python scripts/eval_scenarios.py --scenario policy_and_channel_mixed`
Expected: 打印出 3 行结果（`policy_and_channel_mixed_s42`/`_s101`/`_s202`），无报错。记录下 `predicted`/`confidence`/`runner_up_gap` 这几列的实际值——预期 `predicted` 大概率仍是 `channel_quality_decline`，但 `confidence` 应明显低于 `channel_quality_decline_s42` 那几行（可以顺手跑一下 `PYTHONPATH=. python scripts/eval_scenarios.py --scenario channel_quality_decline` 对比），把两组数字都写进最终报告作为"信号变弱"这个设计意图确实生效的证据。

- [ ] **Step 7: 提交**

```bash
git add data/scenarios.py tests/test_scenarios.py
git commit -m "feat: add weakened-signal policy_and_channel_mixed scenario"
```

---

### Task 4: `tests/test_multi_factor_scenario.py` 多因场景打分验证

**Files:**
- Create: `tests/test_multi_factor_scenario.py`

**Interfaces:**
- Consumes: `data.scenario_runtime.use_scenario_data`（已有）、`data.scenarios._gen_policy_and_channel_mixed`（Task 3 产出）、`agent.hypotheses.build_hypothesis_tree`/`EVIDENCE_RULES`（已有）、`agent.investigator.run_probe`（已有）、`agent.scoring.apply_rules`（已有）。

**背景（为什么不跑完整 `Orchestrator.run()`）：** `run()` 是贪心 best-first 搜索，永远优先展开当前领先假设的 probe。`regulatory_policy_change` 在假设字典（`agent/hypotheses.py::build_hypothesis_tree()`）里排在最后一个，正常搜索顺序下，`channel_quality_decline`（3个probe）+ `approval_policy_change`（2个probe，其中 r5 反证规则大概率触发）+ `data_quality_issue`（2个probe，其中 r8 因为业务知识库内容静态、大概率会误触发）会依次被展开，`MAX_TOOL_CALLS=8` 的预算大概率在轮到 `regulatory_policy_change` 之前就耗尽——它的分数会一直停在 0，测不出这个任务想验证的"两份证据并存时打分不会一边倒"。所以这里直接对两个假设的 probe 分别调 `run_probe()`+`apply_rules()`，跳过 Orchestrator 的搜索顺序，只测打分结果本身，这跟仓库里已有的 `tests/test_regulatory_integration.py` 是同一种做法。

- [ ] **Step 1: 写测试**

创建 `tests/test_multi_factor_scenario.py`：

```python
"""多因交织场景的打分验证：channel_quality_decline 信号调弱后，如果同期
再叠加一份"监管全面收紧"的 web_search mock 内容，两个假设应该都拿到有意义的
分数、且分差明显小于单因清晰场景。

直接调 run_probe()+apply_rules()，不跑完整 Orchestrator.run()——run() 的
best-first 搜索会贪心地先展开当前领先假设，regulatory_policy_change 排在
假设字典最后，正常搜索顺序下轮到它时 MAX_TOOL_CALLS=8 的预算已经被前面
几个假设耗光，永远探测不到，测不出这里想验证的"两份证据并存时分数接近"。
这里直接给两个假设分别喂证据，只测打分结果本身。

跟 data/scenarios.py::_gen_policy_and_channel_mixed 配合使用：那个生成器只
负责渠道信号变弱，这里额外叠加监管信号，两部分合起来才是完整的"多因交织"场景。
"""
import json

import config
from agent.hypotheses import EVIDENCE_RULES, build_hypothesis_tree
from agent.investigator import run_probe
from agent.scoring import apply_rules
from data.scenario_runtime import use_scenario_data
from data.scenarios import _gen_policy_and_channel_mixed

SPLIT_DATE = "2026-07-20"

_MOCK_TIGHTENING_HITS = [
    {
        "title": "监管部门就业务A/业务B类信贷合作业务发布合规通知",
        "url": "https://example.gov.cn/notice/2026-partner-tightening",
        "snippet": "监管部门宣布对助贷业务A、业务B合作模式实施全面收紧措施，资方可分配额度同步下调，"
                   "多家平台反馈新客额度获批规模明显收窄。",
        "published_date": "2026-07-19",
    },
]


def test_weak_channel_signal_plus_regulatory_signal_yields_close_scores(tmp_path, monkeypatch):
    results_path = tmp_path / "mock_web_results.json"
    results_path.write_text(json.dumps(_MOCK_TIGHTENING_HITS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "MOCK_WEB_RESULTS_PATH", str(results_path))

    tree = build_hypothesis_tree()
    df = _gen_policy_and_channel_mixed(seed=42)

    # 渠道信号：喂 channel_quality_decline 的前两个 probe（跟单因清晰场景里
    # test_channel_quality_decline_triggers_share_and_overdue_rules 测的是同一对规则）
    with use_scenario_data(df):
        for probe in tree["channel_quality_decline"].probes[:2]:
            out = run_probe(probe)
            apply_rules(probe.node_id, out["raw_result"], EVIDENCE_RULES, tree, SPLIT_DATE)

    # 监管信号：regulatory_policy_change 的两个 probe，不依赖 CSV 数据，
    # 走 web_search（走上面 monkeypatch 过的 mock 内容）+ 业务知识库检索
    for probe in tree["regulatory_policy_change"].probes:
        out = run_probe(probe)
        apply_rules(probe.node_id, out["raw_result"], EVIDENCE_RULES, tree, SPLIT_DATE)

    channel_score = tree["channel_quality_decline"].score
    regulatory_score = tree["regulatory_policy_change"].score

    assert channel_score > 0
    assert regulatory_score > 0
    assert abs(channel_score - regulatory_score) < 0.25
```

按规则表手算的预期值（供实现时核对，不是断言本身）：渠道侧只有 r1（占比变化>15pp）触发，贡献 `0.30`；监管侧 r9（网络检索到全面收紧信号，权重0.40×置信度0.85）和 r10（业务知识库佐证，权重0.20×置信度0.7）都会触发，贡献 `0.34+0.14=0.48`。两者分差 `0.18`，在阈值 `0.25` 内。

- [ ] **Step 2: 跑测试确认通过（或按实测值调整阈值）**

Run: `PYTHONPATH=. python -m pytest tests/test_multi_factor_scenario.py -v -s`

用 `-s` 保留输出，如果想确认实际分数，可以临时在断言前加 `print(channel_score, regulatory_score)` 观察，确认后可以删掉这行 print（不要把调试用的 print 留在最终提交里）。

Expected: PASS。如果 `abs(channel_score - regulatory_score) < 0.25` 断言失败，说明手算的预期跟实际打分逻辑对不上，需要看实际两个分数分别是多少，把 `0.25` 这个阈值调整到"比 `CONVERGE_MARGIN(0.30)` 明显更小、但比实测分差留一点余量"的合理值，不要为了让测试通过而调到跟实测分差几乎相等（那样以后稍微一点浮动这个测试就会变得脆弱）。

- [ ] **Step 3: 跑全量测试**

Run: `PYTHONPATH=. python -m pytest tests/ -v`
Expected: 全部通过（67 passed，延续 Task 1/2/3 的计数）

- [ ] **Step 4: 提交**

```bash
git add tests/test_multi_factor_scenario.py
git commit -m "test: verify weak channel + regulatory signals yield close hypothesis scores"
```

---

## 完成后整体验证

- [ ] `PYTHONPATH=. python -m pytest tests/ -v` 全部通过（预期 67 passed）
- [ ] `PYTHONPATH=. python scripts/eval_scenarios.py` 跑完全部 16 个场景无报错
- [ ] `python -c "import json; json.load(open('kb/historical_cases.json', encoding='utf-8'))"` 不报错
