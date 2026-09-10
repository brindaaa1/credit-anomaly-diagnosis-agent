"""案例入库 rubric 接到 api.py 后的端到端行为：
诊断跑完后 final 里要带 curation 结果，且按 AUTO_ADMIT_CASES 开关决定要不要真的落盘。

用默认剧情参数（跟 README 里"渠道质量下滑"案例一致）跑真实 Orchestrator，
不 mock 排查过程本身，只把落盘路径 monkeypatch 到临时文件，避免测试写坏真实 kb。
"""
import json

import config
from api import AnomalyEventRequest, DateRange, _JOBS, _run_diagnosis

# 真实种子案例的原文，测试时复制进临时文件用（而不是空列表，见下面注释）
with open(config.CASE_DB_PATH, encoding="utf-8") as _f:
    config_seed_cases_json = _f.read()


def _story_body(event_id: str, phenomenon: str | None = None) -> AnomalyEventRequest:
    return AnomalyEventRequest(
        event_id=event_id,
        metric_name="fpd30_rate",
        anomaly_type="metric_deviation",
        direction="UP",
        baseline_range=DateRange(start="2026-06-01", end="2026-07-19"),
        anomaly_range=DateRange(start="2026-07-20", end="2026-07-30"),
        phenomenon=phenomenon,
    )


# 真实、语义上明显区别于 kb/historical_cases.json 里全部 9 条种子案例的现象描述——
# 用来验证"够新颖 -> high_quality -> 自动入库"这条路径。换成 embedding 检索后，
# R4_novelty 对语义相似的表述很敏感：_build_phenomenon() 兜底拼出来的结构化字段模板
# 反而会被判定为跟 channel_quality_decline 种子案例相似（该模板本来是给 TF-IDF
# 时代设计的取巧文本，见 api.py::_build_phenomenon 的注释），所以这里显式传一个
# 内容上真正新颖的 phenomenon，而不是依赖 fallback 模板。
_NOVEL_PHENOMENON = (
    "某第三方导流渠道单日新增放款用户数环比骤增两倍，"
    "该批用户的征信查询次数普遍高于历史均值，存在批量申贷嫌疑"
)


def _seed_job(diagnosis_id: str) -> None:
    _JOBS[diagnosis_id] = {"status": "pending", "event_id": diagnosis_id, "final": None, "error": None}


def test_run_diagnosis_attaches_curation_and_admits_high_quality_case(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_cases.json"
    cand_path = tmp_path / "case_candidates.json"
    # R4 新颖性要去读 CASE_DB_PATH 做检索，复制真实种子案例而不是空列表——
    # 生产环境本来就一直有种子案例，测试没必要构造这个不现实的空库边界态。
    db_path.write_text(config_seed_cases_json, encoding="utf-8")
    monkeypatch.setattr(config, "CASE_DB_PATH", str(db_path))
    monkeypatch.setattr(config, "CASE_CANDIDATE_PATH", str(cand_path))
    monkeypatch.setattr(config, "AUTO_ADMIT_CASES", True)

    diagnosis_id = "evt-curation-admit"
    _seed_job(diagnosis_id)
    _run_diagnosis(diagnosis_id, _story_body(diagnosis_id, phenomenon=_NOVEL_PHENOMENON))

    original_case_count = len(json.loads(config_seed_cases_json))
    final = _JOBS[diagnosis_id]["final"]
    assert final is not None, _JOBS[diagnosis_id].get("error")
    assert final["curation"]["decision"] == "high_quality"
    assert not cand_path.exists()
    saved = json.loads(db_path.read_text(encoding="utf-8"))
    assert len(saved) == original_case_count + 1
    assert saved[-1]["root_cause_tag"] == "channel_quality_decline"


def test_run_diagnosis_evaluates_but_skips_write_when_auto_admit_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "historical_cases.json"
    cand_path = tmp_path / "case_candidates.json"
    # R4 新颖性要去读 CASE_DB_PATH 做检索，复制真实种子案例而不是空列表——
    # 生产环境本来就一直有种子案例，测试没必要构造这个不现实的空库边界态。
    db_path.write_text(config_seed_cases_json, encoding="utf-8")
    monkeypatch.setattr(config, "CASE_DB_PATH", str(db_path))
    monkeypatch.setattr(config, "CASE_CANDIDATE_PATH", str(cand_path))
    monkeypatch.setattr(config, "AUTO_ADMIT_CASES", False)

    original_case_count = len(json.loads(config_seed_cases_json))
    diagnosis_id = "evt-curation-noauto"
    _seed_job(diagnosis_id)
    _run_diagnosis(diagnosis_id, _story_body(diagnosis_id))

    final = _JOBS[diagnosis_id]["final"]
    # 这个测试只关心"AUTO_ADMIT_CASES=False 时评估仍然发生、但不落盘"，具体是
    # high_quality 还是 candidate 不是它要验证的点（那由上面那个测试覆盖）。
    assert final["curation"]["decision"] in {"high_quality", "candidate"}
    # 但没有落盘：案例库文件内容和运行前一样，candidate 文件压根没被创建
    assert len(json.loads(db_path.read_text(encoding="utf-8"))) == original_case_count
    assert not cand_path.exists()


def test_get_diagnosis_response_exposes_curation_field():
    from api import DiagnosisResultResponse

    assert "curation" in DiagnosisResultResponse.model_fields
