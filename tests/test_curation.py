"""案例入库 rubric 的单测：R1~R4 打分函数 + evaluate_admission 三档决策 + admit_case 落盘。

沿用 test_smoke.py 的风格：真实数据、不 mock，只在涉及文件写入时用 monkeypatch
把落盘路径换成临时文件，避免测试跑坏真实 kb/*.json。
"""
import json

import config
from agent import curation


# ── fixture 构造 ────────────────────────────────────────
def _make_final(*, converged=True, specialized=True, confidence=0.65,
                 runner_up_score=0.20, winner_evidence=None,
                 winner_hyp="channel_quality_decline"):
    if winner_evidence is None:
        winner_evidence = [{
            "rule_id": "r1", "hypothesis": winner_hyp, "desc": "d1", "detail": "x",
            "weight": 0.3, "confidence": 1.0, "contribution": 0.3,
            "source_probe": "p1", "judge_type": "numeric",
        }]
    return {
        "conclusion": "渠道质量下滑", "conclusion_id": winner_hyp, "confidence": confidence,
        "runner_up": ({"desc": "审批口径变化", "score": runner_up_score}
                       if runner_up_score is not None else None),
        "converged": converged, "tool_calls_used": 4, "budget": 8,
        "anomaly_type": "metric_deviation", "direction": "UP",
        "specialized": specialized, "note": None,
        "ranking": [{"hyp_id": winner_hyp, "desc": "渠道质量下滑",
                      "score": confidence, "evidence": winner_evidence}],
    }


def _reflect_probe(evidence_fired):
    return {
        "step_type": "probe", "round": -1, "is_reflect": True,
        "evidence_fired": evidence_fired,
        "probe_id": "p_ap_score", "hypothesis": "approval_policy_change",
        "question": "q", "tool_name": "query_dashboard", "tool_args": {},
        "raw_result": {}, "digest": "d", "folded": False,
    }


def _evidence(judge_type, weight=0.3):
    return {"rule_id": "r", "hypothesis": "channel_quality_decline", "desc": "d",
            "detail": "x", "weight": weight, "confidence": 1.0,
            "contribution": weight, "source_probe": "p", "judge_type": judge_type}


# ── R1 证据链多样性 ──────────────────────────────────────
def test_evidence_diversity_single_judge_type_scores_low():
    final = _make_final(winner_evidence=[_evidence("numeric"), _evidence("numeric")])
    assert curation._evidence_diversity(final) == 0.3


def test_evidence_diversity_two_judge_types_scores_mid():
    final = _make_final(winner_evidence=[_evidence("numeric"), _evidence("llm_judgment")])
    assert curation._evidence_diversity(final) == 0.7


def test_evidence_diversity_three_judge_types_scores_full():
    final = _make_final(winner_evidence=[
        _evidence("numeric"), _evidence("llm_judgment"), _evidence("rag_prior")])
    assert curation._evidence_diversity(final) == 1.0


# ── R2 收敛边际 ──────────────────────────────────────────
def test_margin_score_no_runner_up_is_full_confidence():
    final = _make_final(runner_up_score=None)
    assert curation._margin_score(final) == 1.0


def test_margin_score_saturates_at_half_point_margin():
    final = _make_final(confidence=0.70, runner_up_score=0.20)  # margin 0.50
    assert curation._margin_score(final) == 1.0


def test_margin_score_scales_linearly_below_saturation():
    final = _make_final(confidence=0.45, runner_up_score=0.20)  # margin 0.25
    assert curation._margin_score(final) == 0.5


def test_margin_score_zero_when_tied_or_behind():
    final = _make_final(confidence=0.30, runner_up_score=0.30)
    assert curation._margin_score(final) == 0.0


# ── R3 reflect 稳健性 ────────────────────────────────────
def test_reflect_stability_no_reflect_step_is_neutral():
    trace = [{"step_type": "think", "round": 0, "text": "..."}]
    assert curation._reflect_stability(trace) == 0.6


def test_reflect_stability_clean_reflect_scores_full():
    trace = [_reflect_probe(evidence_fired=[])]
    assert curation._reflect_stability(trace) == 1.0


def test_reflect_stability_reflect_found_counter_evidence_scores_low():
    trace = [_reflect_probe(evidence_fired=[_evidence("llm_judgment")])]
    assert curation._reflect_stability(trace) == 0.3


# ── R4 新颖性（对真实 kb 检索，不 mock） ───────────────────
def test_novelty_low_for_near_duplicate_of_seed_case():
    # 与 kb/historical_cases.json 里 case_2025_11 的 phenomenon 原文几乎一致
    phenomenon = "新增用户FPD30逾期率单周环比上升25%，集中在某新接入渠道"
    assert curation._novelty(phenomenon) < 0.3


def test_novelty_high_for_unrelated_text():
    phenomenon = "英超联赛本赛季冠军由曼城夺得，阿森纳屈居亚军"
    assert curation._novelty(phenomenon) > 0.7


# ── evaluate_admission：门槛 + 三档决策 ─────────────────────
def test_evaluate_admission_rejects_when_not_converged():
    final = _make_final(converged=False)
    result = curation.evaluate_admission("某异常现象", final, [])
    assert result["decision"] == "reject"
    assert result["admission_score"] == 0.0
    assert result["gates"]["converged"] is False


def test_evaluate_admission_rejects_when_not_specialized():
    final = _make_final(specialized=False)
    result = curation.evaluate_admission("某异常现象", final, [])
    assert result["decision"] == "reject"


def test_evaluate_admission_rejects_when_below_confidence_floor():
    final = _make_final(confidence=0.10, runner_up_score=0.05)
    result = curation.evaluate_admission("某异常现象", final, [])
    assert result["decision"] == "reject"


def test_evaluate_admission_rejects_unclassified_conclusion():
    # MIN_CONFIDENCE_FLOOR（0.30，agent/orchestrator.py 里用于标记 other_unclassified）
    # 低于 curation 自己的入库门槛 MIN_CASE_CONFIDENCE（0.5，config.py）——
    # 这条测试锁定"other_unclassified 结论天然过不了入库门槛"这个前提，
    # 不需要改 agent/curation.py 本身。
    final = _make_final(winner_hyp="other_unclassified", confidence=0.10, runner_up_score=0.05)
    result = curation.evaluate_admission("某异常现象", final, [])
    assert result["decision"] == "reject"
    assert result["gates"]["confidence_floor"] is False


def test_evaluate_admission_high_quality_when_all_dimensions_strong():
    final = _make_final(
        confidence=0.80, runner_up_score=0.10,
        winner_evidence=[_evidence("numeric"), _evidence("llm_judgment"), _evidence("rag_prior")],
    )
    trace = [_reflect_probe(evidence_fired=[])]
    result = curation.evaluate_admission("一段全新的、案例库里从未出现过的异常描述文本", final, trace)
    assert result["decision"] == "high_quality"
    assert result["admission_score"] >= 0.7


def test_evaluate_admission_candidate_when_dimensions_mediocre(monkeypatch):
    final = _make_final(
        confidence=0.55, runner_up_score=0.35,  # margin 0.20 -> R2 = 0.4；0.55 过 confidence_floor
        winner_evidence=[_evidence("numeric"), _evidence("llm_judgment")],  # R1 = 0.7
    )
    # 不跑 reflect -> R3 中性 0.6；R4 单独测过，这里锁定一个中等新颖度避免耦合 TF-IDF 具体数值
    monkeypatch.setattr(curation, "_novelty", lambda phenomenon: 0.5)
    result = curation.evaluate_admission("某异常现象", final, [])
    # 手算 admission_score = .35*.7 + .25*.4 + .20*.6 + .20*.5 = 0.565
    assert result["decision"] == "candidate"


# ── admit_case：落盘（monkeypatch 掉真实路径，不许污染真实 kb） ──
def test_admit_case_reject_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_cases.json"
    monkeypatch.setattr(config, "CASE_DB_PATH", str(db_path))
    result = curation.admit_case("现象", _make_final(), {"decision": "reject", "admission_score": 0.0})
    assert result is None
    assert not db_path.exists()


def test_admit_case_high_quality_appends_to_case_db(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_cases.json"
    db_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "CASE_DB_PATH", str(db_path))

    final = _make_final()
    rubric_result = {"decision": "high_quality", "admission_score": 0.85}
    case = curation.admit_case("新的异常现象描述", final, rubric_result)

    assert case["conclusion"] == final["conclusion"]
    assert case["root_cause_tag"] == final["conclusion_id"]
    assert case["case_id"].startswith("case_")

    saved = json.loads(db_path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["case_id"] == case["case_id"]


def test_admit_case_candidate_appends_to_candidate_file_with_pending_status(tmp_path, monkeypatch):
    cand_path = tmp_path / "case_candidates.json"
    monkeypatch.setattr(config, "CASE_CANDIDATE_PATH", str(cand_path))

    final = _make_final()
    rubric_result = {"decision": "candidate", "admission_score": 0.55}
    case = curation.admit_case("现象", final, rubric_result)

    assert case["status"] == "pending"
    saved = json.loads(cand_path.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "pending"
