"""端到端拼缝测试：web_search 工具 -> run_probe -> apply_rules -> 评分。

现有测试各测一段：test_web_search.py 直接调工具，test_llm_client.py 用手搭的
fixture dict，test_hypotheses.py 只看树结构。没有一个测试验证过
"真实的 web_search 返回值经过 run_probe 的 digest 折叠之后，还能以 apply_rules
认得的形状进到评分规则里"——这里补上这条链路，覆盖触发和不触发两种情况。
"""
import json

import config
from agent.hypotheses import EVIDENCE_RULES, build_hypothesis_tree
from agent.investigator import run_probe
from agent.scoring import apply_rules

SPLIT_DATE = "2026-07-20"


def test_regulatory_probe_seam_no_signal_appends_no_evidence():
    """默认 mock 检索结果里没有"全面收紧"这种明确信号，r9 不应触发，
    也就不应该往 evidence_log 里追加任何证据。"""
    tree = build_hypothesis_tree()
    hyp = tree["regulatory_policy_change"]
    probe = next(p for p in hyp.probes if p.node_id == "p_reg_search")

    out = run_probe(probe)

    assert probe.status == "done"
    assert probe.result_digest  # digest 折叠必须产出点东西
    assert out["raw_result"]["tool_name"] == "web_search"

    fired = apply_rules("p_reg_search", out["raw_result"], EVIDENCE_RULES, tree, SPLIT_DATE)

    assert fired == []
    assert hyp.evidence_log == []
    assert hyp.score == 0.0


def test_regulatory_probe_seam_explicit_signal_fires_evidence(monkeypatch, tmp_path):
    """检索结果里出现明确的"全面收紧"信号时，链路走完后应该在
    hypothesis 的 evidence_log 里看到 r9 触发的条目，分数相应增加。"""
    mock_results = [
        {
            "title": "监管部门宣布对助贷行业实施全面收紧措施",
            "url": "https://example.gov.cn/notice/2026-tightening",
            "snippet": "监管部门宣布对助贷行业实施全面收紧措施，多项准入及审批要求即刻生效。",
            "published_date": "2026-07-21",
        },
    ]
    results_path = tmp_path / "mock_web_results.json"
    results_path.write_text(json.dumps(mock_results, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "MOCK_WEB_RESULTS_PATH", str(results_path))

    tree = build_hypothesis_tree()
    hyp = tree["regulatory_policy_change"]
    probe = next(p for p in hyp.probes if p.node_id == "p_reg_search")

    out = run_probe(probe)
    fired = apply_rules("p_reg_search", out["raw_result"], EVIDENCE_RULES, tree, SPLIT_DATE)

    assert len(fired) == 1
    assert fired[0]["rule_id"] == "r9"
    assert fired[0] in hyp.evidence_log
    assert hyp.score == fired[0]["contribution"] > 0
