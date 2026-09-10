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
    # 直接对齐"分差明显小于单因清晰场景"想验证的真实机制：orchestrator 用
    # CONVERGE_MARGIN 判断是否收敛，分差小于它才谈得上"系统会犹豫不决"。
    assert abs(channel_score - regulatory_score) < config.CONVERGE_MARGIN
