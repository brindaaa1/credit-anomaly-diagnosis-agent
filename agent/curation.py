"""案例入库 rubric：一次诊断结束后，判断值不值得沉淀成案例库里的新案例。

不是训练用的 reward，是准入门槛——呼应 EVIDENCE_RULES 的打分卡风格：
硬门槛（Gate）挡掉不合格的结论，加权 rubric 再把过了门槛的结论分成
high_quality（自动写入 kb/historical_cases.json，下次检索直接生效）和
candidate（写入 kb/case_candidates.json，等人工用 scripts/review_candidates.py 确认）。

不放进 Orchestrator：Orchestrator 只读工具、只做推理，是个纯函数式的排查过程；
要不要把结果写成案例是"诊断服务"这层的策略决定，混进去会破坏 Orchestrator
"相同输入产出相同结论"的可测试性。调用方是 api.py 的 _run_diagnosis()。
"""
import json
import os
import threading
import uuid

import config

CURATION_RUBRIC = [
    {"id": "R1_evidence_diversity", "weight": 0.35},
    {"id": "R2_margin", "weight": 0.25},
    {"id": "R3_reflect_stability", "weight": 0.20},
    {"id": "R4_novelty", "weight": 0.20},
]

_CURATION_LOCK = threading.Lock()


# ── R1: 证据链多样性 ────────────────────────────────────
def _evidence_diversity(final: dict) -> float:
    """只靠一条大权重规则撑起来的结论不适合当范本；
    多种 judge_type（numeric/llm_judgment/rag_prior/external_prior）互相印证的才是好案例。"""
    winner = final["ranking"][0]
    judge_types = {e["judge_type"] for e in winner["evidence"]}
    if len(judge_types) <= 1:
        return 0.3
    if len(judge_types) == 2:
        return 0.7
    return 1.0


# ── R2: 收敛边际 ────────────────────────────────────────
def _margin_score(final: dict) -> float:
    runner_up = final.get("runner_up")
    if runner_up is None:
        return 1.0
    margin = final["confidence"] - runner_up["score"]
    return round(max(0.0, min(margin / 0.5, 1.0)), 3)


# ── R3: reflect 自我核验有没有撼动结论 ───────────────────
def _reflect_stability(trace: list) -> float:
    """没跑 reflect（次优假设已无 pending probe）给中性分；
    跑了但没触发任何反证证据 -> 干净通过；跑了且触发了证据 -> 有不利信号，值得警惕。
    这跟 R2 的边际大小是正交的：R2 看结果多稳，R3 看有没有真的做过自我核验并通过。"""
    reflect_probe = next(
        (s for s in trace if s.get("step_type") == "probe" and s.get("is_reflect")), None
    )
    if reflect_probe is None:
        return 0.6
    if not reflect_probe.get("evidence_fired"):
        return 1.0
    return 0.3


# ── R4: 新颖性（复用现有检索工具，不新写检索逻辑） ─────────
# BGE embedding 的余弦相似度有个语义"地板"：即使两条毫不相关的案例，相似度也很少低于 0.4。
# 用 kb/historical_cases.json 里 9 条真实种子案例两两算过相似度，不相关案例对的范围是
# 0.441~0.745（中位数 0.579）。_SIM_FLOOR/_SIM_CEILING 就是照这个分布定的，把这段"噪音区间"
# 压到 novelty≈1，只有真正超出该区间上沿（接近复述/重复案例）才会让 novelty 明显走低。
_SIM_FLOOR = 0.45
_SIM_CEILING = 0.90


def _novelty(phenomenon: str) -> float:
    from tools.retrieval import retrieve_historical_case

    result = retrieve_historical_case(phenomenon, top_k=1)
    if not result["hits"]:
        return 1.0
    similarity = result["hits"][0]["similarity"]
    normalized = (similarity - _SIM_FLOOR) / (_SIM_CEILING - _SIM_FLOOR)
    return round(1.0 - max(0.0, min(normalized, 1.0)), 3)


# ── 汇总：门槛 + 加权分 + 三档决策 ─────────────────────────
def evaluate_admission(phenomenon: str, final: dict, trace: list) -> dict:
    gates = {
        "converged": bool(final.get("converged")),
        "specialized": bool(final.get("specialized")),
        "confidence_floor": final.get("confidence", 0.0) >= config.MIN_CASE_CONFIDENCE,
    }
    if not all(gates.values()):
        return {"decision": "reject", "gates": gates, "scores": {}, "admission_score": 0.0}

    scores = {
        "R1_evidence_diversity": _evidence_diversity(final),
        "R2_margin": _margin_score(final),
        "R3_reflect_stability": _reflect_stability(trace),
        "R4_novelty": _novelty(phenomenon),
    }
    admission_score = round(
        sum(scores[r["id"]] * r["weight"] for r in CURATION_RUBRIC), 3
    )
    if admission_score >= 0.7:
        decision = "high_quality"
    elif admission_score >= 0.45:
        decision = "candidate"
    else:
        decision = "reject"
    return {"decision": decision, "gates": gates, "scores": scores, "admission_score": admission_score}


# ── 落盘 ─────────────────────────────────────────────────
def _summarize_evidence_chain(final: dict) -> str:
    winner = final["ranking"][0]
    steps = [f"{e['desc']}（{e['detail']}）" for e in winner["evidence"] if e.get("detail")]
    return " -> ".join(steps) if steps else winner["desc"]


def _append_json(path: str, item: dict) -> None:
    with _CURATION_LOCK:
        items = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
        items.append(item)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)


def admit_case(phenomenon: str, final: dict, rubric_result: dict) -> dict | None:
    """按 rubric_result['decision'] 落盘。调用方应先判断 decision != "reject" 再调用，
    这里对 reject 也做了防御性处理，返回 None 且不写任何文件。"""
    if rubric_result["decision"] == "reject":
        return None

    case = {
        "case_id": f"case_{uuid.uuid4().hex[:10]}",
        "phenomenon": phenomenon,
        "investigation": _summarize_evidence_chain(final),
        "conclusion": final["conclusion"],
        "root_cause_tag": final["conclusion_id"],
        "admission_score": rubric_result["admission_score"],
    }
    if rubric_result["decision"] == "high_quality":
        _append_json(config.CASE_DB_PATH, case)
    else:  # candidate
        case["status"] = "pending"
        _append_json(config.CASE_CANDIDATE_PATH, case)
    return case
