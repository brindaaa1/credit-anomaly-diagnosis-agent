"""CI 冒烟测试：只保证"能跑通"，不是完整单测覆盖。

跑之前需要先执行 data/generate_mock_data.py 生成 mock 数据
（CI workflow 里已经这么做了）。MOCK_LLM 默认就是 "1"，不用单独设置。
"""
from agent.hypotheses import HYP_LABELS
from agent.orchestrator import Orchestrator

VALID_CONCLUSIONS = set(HYP_LABELS)


def test_orchestrator_runs_end_to_end():
    final = Orchestrator().run()
    assert final["conclusion_id"] in VALID_CONCLUSIONS
    assert 0 <= final["confidence"]
    assert final["tool_calls_used"] <= final["budget"]


def test_regulatory_hypothesis_does_not_disturb_default_story():
    """新增的 regulatory_policy_change 假设分支不应该改变默认剧情的结论——
    默认剧情在 round 1 就因为 channel_quality_decline 的 margin 超过
    CONVERGE_MARGIN 提前收敛，regulatory 分支的 probe 根本不会被展开。"""
    orch = Orchestrator()
    final = orch.run()
    assert final["conclusion_id"] == "channel_quality_decline"
    reg = orch.tree["regulatory_policy_change"]
    assert all(p.status == "pending" for p in reg.probes)


def test_api_app_importable():
    from api import app
    assert app.title
