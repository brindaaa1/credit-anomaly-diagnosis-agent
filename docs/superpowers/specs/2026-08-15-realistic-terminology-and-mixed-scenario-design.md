# 风控黑话落地 + 多因交织场景 设计

## 背景

两个反馈驱动这次改动：

1. **测试/案例太"干净"**：`data/scenarios.py` 的 5 类场景（含 `ambiguous_negative`）每类都只有一个孤立的真因，信号强度调得足以稳定触发对应规则、不误触发其他规则。真实业务里的异常经常是多个原因同时发生、互相干扰，agent 更应该被"信号打架时敢不敢老实说不确定"这种场景考验，而不是只考"信号很干净时能不能识别"。
2. **案例/术语偏教科书**：`kb/business_kb.md`、`kb/historical_cases.json` 里的案例描述用词规整但缺少真实业务里常说的黑话，读起来不像一线风控人员会写的东西。

用户提供的真实术语（脱敏后）：1m30 / 1m30_amount（按期数、金额口径的逾期指标通用命名）、睡眠用户、找黑能力、标的/发标、新客额度/老客额度、期数金额加权、戳额、多头。业务线统一脱敏为"业务A/业务B/业务C"。

用户提供的两个真实"多因交织"场景方向：
- 监管政策变化压低了资方可分配额度，与渠道结构变化同期发生，两个原因互相干扰排查
- 复借老客还款表现变差是因为首借有地推人员对接、复借没有——这是现有 5 个假设都装不下的新根因类型，本轮**明确不做**，只记录为后续待办

## 目标

1. 在 `kb/business_kb.md` 里补充新术语词条，风格跟现有 `FPD30`/`审批线`/`渠道` 几条一致（简短、可被 TF-IDF 检索命中）
2. 给 `kb/historical_cases.json` 里 4 条原始种子案例（`case_2025_11`/`case_2025_08`/`case_2026_02`/`case_2025_05`）的文案做轻量润色，自然嵌入新术语（后加的 5 条 Codex 数据质量案例已经足够真实，不动）
3. 新增一个"多因交织"场景生成器，复用现有 `channel_quality_decline`（渠道结构+信用分+逾期率）机制但调弱信号强度，纳入 `SCENARIOS` 注册表，参与常规批量评估
4. 新增一个专门测试，验证"当渠道信号和监管信号都不够强但同时存在时，agent 给出的两个假设分数应该接近，而不是自信地只咬定一个"——这是对上一轮刚做的置信度地板/reflect 机制的真实场景验证

## 非目标

- 不新增第 6 个假设类型（地推/复借服务触达变化）——现有 5 个假设都装不下这个真因，作为后续独立子项目处理，这次不碰 `agent/hypotheses.py` 的假设树结构
- 不改 `agent/hypotheses.py` 里 Hypothesis 的 `description`/probe 的 `question` 文案——这些文本会流进 `final["conclusion"]`，`BACKEND_INTEGRATION.md` 里有引用它们的示例字符串，这次不动，把黑话集中加在 KB/案例文案层，风险更小
- 不改 README.md 的"埋点故事线"叙述
- 不改任何 CSV schema 字段名（`channel`/`credit_score` 等英文列名不变），黑话只出现在中文文案里
- 不改 `agent/llm_client.py::_mock_judgment` 的关键词匹配逻辑本身——新场景的 mock 检索内容设计成能匹配现有关键词（比如复用"全面收紧"这个 r9 已经在认的词），不新增匹配分支
- 不扩展 `scripts/eval_scenarios.py`/`ScenarioSpec` 的评分语义（比如不引入"多真值"或"模糊命中"的新字段）——新场景在批量评估表里就按单一 `ground_truth`（`channel_quality_decline`）算命中/未命中，"两个假设分数接近"这个属性用单独的专项测试验证，不污染通用评估框架

## 架构

```
kb/business_kb.md          改：新增 8 条术语词条
kb/historical_cases.json   改：4 条种子案例文案润色
data/scenarios.py          改：新增 _gen_policy_and_channel_mixed() + 注册进 SCENARIOS
tests/test_scenarios.py    改：追加新场景的信号强度断言（沿用现有测试风格）
tests/test_multi_factor_scenario.py   新增：打分层验证"多因场景下两个假设分数接近"
```

不改 `agent/orchestrator.py`、`agent/scoring.py`、`agent/hypotheses.py`、`data/scenario_runtime.py`、`scripts/eval_scenarios.py`——这些文件的现有能力已经足够支撑这次改动，复用即可。

## 组件详细设计

### 1. `kb/business_kb.md` 新增词条

风格对齐现有 6 条核心词条（简短、陈述式，方便 TF-IDF 检索命中），插入位置：紧跟在现有"## 监管政策"词条之后，`## Vintage报表(3)...`之前（即插在"结构化短词条"区块的末尾，长篇参考文章区块之前）：

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

### 2. `kb/historical_cases.json` 案例文案润色

只改 4 条种子案例（其余 5 条 Codex 数据质量案例已经很真实，不动）。逐条给出旧→新：

**`case_2025_11`（渠道质量下滑）**
```json
"phenomenon": "新增用户1m30逾期率单周环比上升25%，集中在某新接入渠道，且该渠道新客多头占比偏高",
"investigation": "按渠道拆解逾期率 -> 发现渠道X占比从8%升到30% -> 对比该渠道用户信用分分布，均值低于大盘35分 -> 叠加多头借贷数据后发现该渠道新客多头占比显著高于大盘",
"conclusion": "渠道质量下滑：新渠道放量且用户资质偏弱（多头占比高），建议对该渠道新客额度单独收紧"
```

**`case_2025_08`（宏观季节性）**
```json
"investigation": "对比历史三年同期 -> 每年春节后均有类似抬升，幅度一致 -> 渠道结构和用户画像无变化 -> 同期用户戳额（查看额度）频次同步上升，佐证是集中的短期还款压力而非渠道结构问题"
```
（`phenomenon`/`conclusion` 不改，只在 `investigation` 里加一步佐证）

**`case_2026_02`（数据质量问题）**
```json
"phenomenon": "某日1m30逾期率数值跳变翻倍，次日恢复正常"
```
（只把"FPD30"改成更通用的"1m30"说法，其余不变，这条本来就足够真实）

**`case_2025_05`（审批口径变化）**
```json
"phenomenon": "逾期率缓慢爬升一个月，新客额度对应用户群的信用分分布明显左移",
"investigation": "检查审批策略变更记录 -> 发现风控为冲量下调新客额度对应审批线20分 -> 低分段用户占比上升",
"conclusion": "审批口径变化：新客额度审批线下调引入更高风险用户，属预期内风险抬升"
```

### 3. `data/scenarios.py` 新增多因交织场景

新增生成函数，紧跟在 `_gen_channel_quality_decline` 之后：

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

注册进 `SCENARIOS`：

```python
"policy_and_channel_mixed": ScenarioSpec(
    "policy_and_channel_mixed", "channel_quality_decline",
    "新增用户1m30逾期率近期上升，渠道结构和监管环境同期都有变化，信号强度弱于典型场景",
    _gen_policy_and_channel_mixed, (42, 101, 202)),
```

`ground_truth` 定为 `channel_quality_decline`——这是这份 DataFrame 本身唯一编码进去的真实机制；监管信号只在专项测试里通过 mock 检索内容叠加，不属于这份数据生成器的职责。三个随机种子变体沿用现有惯例。

具体的偏移量（+16pp、-20分、+4pp）以"r1 大概率触发、r2 大概率不触发"为验收标准，实现时按现有 `sample_day()` 同款正态分布参数推算验证，跟原 scenario-eval-harness 设计文档的做法一致。

### 4. `tests/test_scenarios.py` 追加信号强度断言

在文件末尾追加一个测试函数，沿用文件里已有的 `_query` 辅助函数：

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

（需要在文件顶部的 import 列表里加上 `_gen_policy_and_channel_mixed`，参照现有 5 个生成函数的 import 方式）

### 5. `tests/test_multi_factor_scenario.py`（新增文件）

**不跑完整的 `Orchestrator.run()`**——推演过执行细节后发现：`run()` 是贪心 best-first 搜索，永远优先展开当前领先假设的 probe；`regulatory_policy_change` 在假设字典里排最后，实测按正常搜索顺序，等轮到它展开时 `MAX_TOOL_CALLS=8` 的预算早就被 `channel_quality_decline`（3个probe）+ 排在前面的 `approval_policy_change`（2个probe，r5会触发反证）+ `data_quality_issue`（2个probe，r8因KB静态内容会误触发）耗光了，`regulatory_policy_change` 永远轮不到、分数会一直是 0——测不出"两个假设分数接近"这个设计意图。

所以这里改成跟 `tests/test_regulatory_integration.py` 一样的做法：直接对两个假设的 probe 分别调 `run_probe()` + `apply_rules()`，跳过 Orchestrator 的搜索顺序，只验证"两份证据都摆出来之后，打分结果是否接近"这个核心问题：

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

按规则表手算一遍预期值（实现阶段以实际跑出的数字为准，这里只是验证设计可行）：渠道侧只有 r1（占比变化>15pp）会触发，r2（渠道逾期率高出均值50%）预期不触发（`+16pp`占比变化、`+4pp`逾期风险加成都刻意调得不够强）——贡献 `0.30`；监管侧 r9（网络检索到全面收紧信号）和 r10（业务知识库佐证，KB 里"监管政策"词条内容本身是静态的"政策收紧式调整"表述，参照 `scripts/eval_scenarios.py` 文档里对 r10 类似静态 KB 命中已有的说明）都会触发——贡献 `0.34+0.14=0.48`。两者分差 `0.18`，在阈值 `0.25` 内。

## 数据流

```
data/scenarios.py::_gen_policy_and_channel_mixed(seed)
  → 弱化版渠道数据 DataFrame（channel_C 信号刚过 r1 阈值，不足以触发 r2）

场景1：走 scripts/eval_scenarios.py 常规批量评估（完整 Orchestrator.run()，走真实 best-first 搜索）
  → 只用上面的 DataFrame，web_search 走默认 mock（不含"全面收紧"）
  → channel_quality_decline 能拿到的最多是 r1（+可能 r3，取决于历史案例检索是否命中），
    regulatory_policy_change 因为排在假设字典最后、搜索预算大概率耗尽前轮不到它，预期分数为 0 或很低
  → 体现"同一个真因，但证据比单因清晰场景弱、最终置信度更低"，具体分数以实测为准

场景2：走 tests/test_multi_factor_scenario.py 专项测试（直接调 run_probe+apply_rules，不跑完整 Orchestrator.run()）
  → 上面的 DataFrame（喂 channel_quality_decline 的 2 个 probe）
    + 额外 monkeypatch 的"全面收紧" web_search mock 内容（喂 regulatory_policy_change 的 2 个 probe）
  → channel_quality_decline 拿到 r1 证据（0.30分），regulatory_policy_change 拿到 r9+r10 证据（约0.48分）
  → 两个假设分数接近（分差远小于 CONVERGE_MARGIN=0.30）→ 验证"两份证据并存时打分不会一边倒"
```

## 错误处理

沿用现有工具/测试的错误处理约定，这次不新增任何新的失败路径：`use_scenario_data()`（已有，Task 1 built）保证数据源正常恢复；`monkeypatch` 对 `config.MOCK_WEB_RESULTS_PATH` 的修改由 pytest 在测试结束时自动恢复，不需要额外的 try/finally。

## 测试

- `tests/test_scenarios.py`：新增 1 个断言（上面第 4 节），验证新场景的信号强度符合"r1 触发、r2 不触发"的设计意图
- `tests/test_multi_factor_scenario.py`（新增文件）：1 个打分层测试（直接调 `run_probe`+`apply_rules`，不跑完整 `Orchestrator.run()`，原因见第 5 节），验证多因交织场景下两个假设分数接近
- 全量跑一遍 `PYTHONPATH=. python -m pytest tests/ -v`，确认不破坏现有 64 个用例
- 手动跑一遍 `PYTHONPATH=. python scripts/eval_scenarios.py --scenario policy_and_channel_mixed`，确认新场景能正常产出结果行（3 个种子变体），预期 `predicted` 大概率仍是 `channel_quality_decline` 但 `confidence`/`runner_up_gap` 明显低于 `channel_quality_decline_s42` 等原场景——这是"信号变弱"这个设计意图的直接证据，记录到实现报告里

## 已知限制

- `policy_and_channel_mixed` 场景在常规批量评估（`scripts/eval_scenarios.py`）里体现不出完整的"双假设模糊"，只体现"单一信号变弱"——因为 `web_search` 的 mock 内容是全局静态文件，不像 `config.DATA_PATH` 那样有 `use_scenario_data()` 这类按场景切换的机制。完整的模糊场景验证只能通过专项测试（`tests/test_multi_factor_scenario.py`）达成。如果以后想让批量评估也能覆盖这种双假设模糊场景，需要给 `ScenarioSpec` 加一个可选的 web mock 内容字段、`run_one()` 相应改造——这次评估后决定不做（见"非目标"），留作后续方向。
- 新增的黑话词条在 `MOCK_LLM=1` 模式下不会改变任何一次排查的实际打分结果（已验证）；但新增的 KB 词条会影响部分 probe 固定 query 的 TF-IDF 检索排序（例如 `1m30 / 1m30_amount` 词条现在会进入 `p_dq_kb` 查询的检索结果前几名），真实 LLM 模式下这可能改变 llm_judgment 类规则实际看到的 KB 摘要内容，这次没有验证真实模式下的影响。

## 不在本次范围内（后续可能的方向）

- 第 6 个假设类型：地推/复借服务触达变化导致复借表现变差
- `agent/hypotheses.py` 里 Hypothesis 描述文案/probe question 文案的黑话润色
- `ScenarioSpec` 支持按场景切换 web_search mock 内容，让批量评估也能覆盖双假设模糊场景
- README.md 故事线叙述的黑话润色
