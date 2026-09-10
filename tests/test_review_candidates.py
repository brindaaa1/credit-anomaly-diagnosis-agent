"""candidate 档人工复核脚本的纯逻辑单测。

只测 apply_decision / promote_to_library 这两个不依赖交互输入的函数——
main() 的 input() 循环是胶水代码，跟 api.py 的路由处理函数一样不做单测，
交互流程靠手工跑一遍验证（README 会写怎么跑）。
"""
import json

import config
from scripts.review_candidates import apply_decision, load_candidates, promote_to_library, save_candidates


def _candidate(case_id="case_abc123", status="pending"):
    return {
        "case_id": case_id, "phenomenon": "现象", "investigation": "排查过程",
        "conclusion": "渠道质量下滑", "root_cause_tag": "channel_quality_decline",
        "admission_score": 0.55, "status": status,
    }


def test_apply_decision_approve_marks_status_and_returns_promotable_case():
    candidates = [_candidate()]
    result = apply_decision(candidates, "case_abc123", "approve")
    assert result["found"] is True
    assert result["promoted_case"]["case_id"] == "case_abc123"
    assert "status" not in result["promoted_case"]  # 入库的案例不带 candidate 专属字段
    assert candidates[0]["status"] == "approved"  # 原地更新，留痕而不是删除


def test_apply_decision_reject_marks_status_without_promoting():
    candidates = [_candidate()]
    result = apply_decision(candidates, "case_abc123", "reject")
    assert result["found"] is True
    assert result["promoted_case"] is None
    assert candidates[0]["status"] == "rejected"


def test_apply_decision_unknown_case_id_not_found():
    candidates = [_candidate()]
    result = apply_decision(candidates, "case_does_not_exist", "approve")
    assert result["found"] is False
    assert candidates[0]["status"] == "pending"  # 未被误改


def test_apply_decision_already_reviewed_case_is_not_reprocessed():
    candidates = [_candidate(status="approved")]
    result = apply_decision(candidates, "case_abc123", "reject")
    assert result["found"] is False
    assert candidates[0]["status"] == "approved"  # 已审过的不会被二次改写


def test_promote_to_library_appends_to_case_db(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_cases.json"
    db_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "CASE_DB_PATH", str(db_path))

    promote_to_library({"case_id": "case_abc123", "conclusion": "渠道质量下滑",
                         "root_cause_tag": "channel_quality_decline"})

    saved = json.loads(db_path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["case_id"] == "case_abc123"


def test_load_and_save_candidates_roundtrip(tmp_path, monkeypatch):
    cand_path = tmp_path / "case_candidates.json"
    monkeypatch.setattr(config, "CASE_CANDIDATE_PATH", str(cand_path))

    assert load_candidates() == []  # 文件不存在时返回空列表，不报错

    save_candidates([_candidate()])
    assert load_candidates() == [_candidate()]
