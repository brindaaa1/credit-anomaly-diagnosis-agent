"""假设树的第 5 个分支：regulatory_policy_change。
只测树的结构和 query 的动态拼接——打分行为已经在 test_llm_client.py /
test_scoring 覆盖过，这里不重复。
"""
from agent.hypotheses import EVIDENCE_RULES, build_hypothesis_tree


def test_tree_includes_regulatory_policy_change_branch():
    tree = build_hypothesis_tree()
    assert "regulatory_policy_change" in tree
    hyp = tree["regulatory_policy_change"]
    assert [p.node_id for p in hyp.probes] == ["p_reg_search", "p_reg_kb"]
    assert hyp.probes[0].tool_name == "web_search"
    assert hyp.probes[1].tool_name == "retrieve_business_kb"


def test_regulatory_search_query_is_anchored_to_anomaly_range():
    tree_july = build_hypothesis_tree(anomaly_range="2026-07-20~2026-07-30")
    tree_march = build_hypothesis_tree(anomaly_range="2026-03-01~2026-03-10")

    query_july = tree_july["regulatory_policy_change"].probes[0].tool_args["query"]
    query_march = tree_march["regulatory_policy_change"].probes[0].tool_args["query"]

    assert "2026-07" in query_july
    assert "2026-03" in query_march
    assert query_july != query_march


def test_evidence_rules_include_regulatory_entries():
    rule_ids = {r["rule_id"] for r in EVIDENCE_RULES}
    assert {"r9", "r10"} <= rule_ids
    r9 = next(r for r in EVIDENCE_RULES if r["rule_id"] == "r9")
    assert r9["hypothesis"] == "regulatory_policy_change"
    assert r9["weight"] == 0.40
    assert r9["type"] == "llm_judgment"
    assert r9["probe_id"] == "p_reg_search"


def test_hyp_labels_cover_all_tree_hypotheses_plus_fallback():
    from agent.hypotheses import HYP_LABELS
    tree = build_hypothesis_tree()
    assert set(tree) <= set(HYP_LABELS)
    assert "other_unclassified" in HYP_LABELS
