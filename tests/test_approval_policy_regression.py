"""回归测试：approval_policy_change 场景必须能被正确归因，且不能靠误伤别的假设赢。

2026-08-16 用 scripts/eval_scenarios.py 实测发现两个独立 bug，合起来导致这个场景
3/3 全部误判为 other_unclassified（准确率 80%）：
1. r5（"KB无变更记录"反证，旧权重 -0.30）几乎必然触发，抵消掉 r4（信用分实测
   下降23分，权重 +0.35）的正确信号——已把权重调到 -0.10。
2. r3（channel_quality_decline 的历史案例检索规则）当时 query_text 写死，不管
   真实数据是什么永远查同一句话，在这个场景里凭空送 +0.17 分噪声——已改成
   phenomenon 驱动检索 + agent/llm_client.py::_mock_judgment 收紧到只看 top-1。
eval_scenarios.py 是手动调优工具、不接入 CI，这类端到端误判不会被已有的单条规则
触发测试（tests/test_scenarios.py）或 _finalize() 单元测试（tests/test_orchestrator.py）
捕获——那些测试要么只测单条规则是否触发，要么直接喂手工分数绕过了真实证据链的
加总。这里跑一次真实 Orchestrator，分别锁住"该赢的假设必须赢"和"不该触发的
规则不能触发"这两层结果，防止两个 bug 中任何一个再次静默回归。
"""
from agent.orchestrator import Orchestrator
from data.scenario_runtime import use_scenario_data
from data.scenarios import SCENARIOS


def test_approval_policy_change_scenario_is_correctly_attributed():
    spec = SCENARIOS["approval_policy_change"]
    df = spec.generator(42)
    with use_scenario_data(df):
        final = Orchestrator().run(phenomenon=spec.phenomenon)
    assert final["conclusion_id"] == "approval_policy_change"
    assert final["confidence"] >= 0.30  # 不能卡在置信度地板以下


def test_channel_quality_decline_r3_does_not_fire_without_real_channel_signal():
    """r5 权重修复后，光靠它就已经能让 approval_policy_change 正确赢下这个场景
    （见上面那个测试），单看最终结论掩盖了另一个独立 bug：r3（channel_quality_decline
    的历史案例检索规则）当时用的是写死的 query_text，不管真实数据是什么永远查
    同一句话、命中同一条案例，在这个场景里照样以 0.85 置信度触发，凭空送
    +0.17 分——只是当时不够翻盘。已改成 phenomenon 驱动检索
    + agent/llm_client.py::_mock_judgment 收紧到只看 top-1（而不是整个 top-k
    blob 里搜子串），这里直接断言 r3 在没有真实渠道信号的场景里不应该触发。
    """
    spec = SCENARIOS["approval_policy_change"]
    df = spec.generator(42)
    with use_scenario_data(df):
        final = Orchestrator().run(phenomenon=spec.phenomenon)
    channel_hyp = next(h for h in final["ranking"] if h["hyp_id"] == "channel_quality_decline")
    fired_rules = {e["rule_id"] for e in channel_hyp["evidence"]}
    assert "r3" not in fired_rules
