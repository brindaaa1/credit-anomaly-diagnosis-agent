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
