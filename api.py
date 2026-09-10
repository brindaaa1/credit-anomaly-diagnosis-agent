"""对接 Java 检测层的 HTTP 接口层。

两个接口：
  POST /api/v1/diagnoses            接收异常事件，创建排查任务，返回 diagnosisId
  GET  /api/v1/diagnoses/{id}       查排查状态/结论

Orchestrator.run() 是同步阻塞调用（内部多轮工具调用 + LLM 判断），
用 BackgroundTasks 丢到线程池里跑，避免 Java 那边同步等一个不确定时长的请求。
任务状态先放进程内 dict：demo/单机场景够用，重启会丢、多进程不共享，
以后要扩容再换 Redis/DB，不在这次改动范围内。

运行: uvicorn api:app --reload
"""
import threading
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

import config
from agent.curation import admit_case, evaluate_admission
from agent.orchestrator import Orchestrator

app = FastAPI(title="信贷数据异常排查 Agent")

AnomalyType = Literal["metric_deviation", "model_degradation", "distribution_shift"]
Direction = Literal["UP", "DOWN", "SHIFT"]
_DIRECTION_CN = {"UP": "上升", "DOWN": "下降", "SHIFT": "偏移"}


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DateRange(_CamelModel):
    start: str
    end: str

    def as_range_str(self) -> str:
        return f"{self.start}~{self.end}"


class AnomalyEventRequest(_CamelModel):
    event_id: str
    metric_name: str
    anomaly_type: AnomalyType
    direction: Direction
    baseline_range: DateRange
    anomaly_range: DateRange
    # Orchestrator.run() 不消费检测时间，Java 侧的六字段契约里本就不含它，设为可选/忽略多传的值
    detected_at: str | None = None
    # Java 检测层暂未实现细分定位，先留空可选字段，接入时无需改这里
    suspect_segments: list[dict] = Field(default_factory=list)
    # 可选：调用方如果能提供一句真实的异常现象描述（而不是靠 _build_phenomenon 从结构化字段
    # 拼模板），历史案例 RAG 检索（含 R4_novelty）的匹配质量会明显更好。六字段契约不含它，
    # Java 侧继续不传也完全兼容，走 _build_phenomenon 的 fallback。
    phenomenon: str | None = None


class DiagnosisJobResponse(_CamelModel):
    diagnosis_id: str
    event_id: str
    status: Literal["pending", "running", "done", "error"]


class DiagnosisResultResponse(_CamelModel):
    diagnosis_id: str
    event_id: str
    status: Literal["pending", "running", "done", "error"]
    error: str | None = None
    conclusion_id: str | None = None
    conclusion: str | None = None
    confidence: float | None = None
    low_confidence: bool | None = None
    anomaly_type: str | None = None
    direction: str | None = None
    specialized: bool | None = None
    note: str | None = None
    converged: bool | None = None
    tool_calls_used: int | None = None
    budget: int | None = None
    ranking: list[dict] | None = None
    curation: dict | None = None


# ── 任务状态存储（进程内，见文件头说明） ──────────────────
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _build_phenomenon(body: AnomalyEventRequest) -> str:
    """把结构化字段拼成一句中文现象描述，供 body.phenomenon 未传时兜底——
    Java 六字段契约里没有自然语言描述，这段模板文本的检索匹配质量天然有限，
    调用方能传 phenomenon 的话应该优先传。"""
    direction_cn = _DIRECTION_CN.get(body.direction, body.direction)
    return (
        f"{body.metric_name} 近期{direction_cn}（{body.anomaly_type}），"
        f"基线期 {body.baseline_range.as_range_str()}，"
        f"异常期 {body.anomaly_range.as_range_str()}"
    )


def _run_diagnosis(diagnosis_id: str, body: AnomalyEventRequest) -> None:
    with _LOCK:
        _JOBS[diagnosis_id]["status"] = "running"
    try:
        orch = Orchestrator()
        phenomenon = body.phenomenon or _build_phenomenon(body)
        final = orch.run(
            phenomenon=phenomenon,
            metric_name=body.metric_name,
            baseline_range=body.baseline_range.as_range_str(),
            anomaly_range=body.anomaly_range.as_range_str(),
            direction=body.direction,
            anomaly_type=body.anomaly_type,
            suspect_segments=body.suspect_segments or None,
        )
        # 案例入库 rubric：评估结果始终附在 final 里（即使不自动落盘也方便观测），
        # 只有 AUTO_ADMIT_CASES 开着且过了门槛才真的写文件，见 agent/curation.py。
        rubric_result = evaluate_admission(phenomenon, final, orch.trace)
        if config.AUTO_ADMIT_CASES and rubric_result["decision"] != "reject":
            admit_case(phenomenon, final, rubric_result)
        final["curation"] = rubric_result
        with _LOCK:
            _JOBS[diagnosis_id]["status"] = "done"
            _JOBS[diagnosis_id]["final"] = final
    except Exception as e:
        with _LOCK:
            _JOBS[diagnosis_id]["status"] = "error"
            _JOBS[diagnosis_id]["error"] = str(e)


@app.post("/api/v1/diagnoses", response_model=DiagnosisJobResponse, status_code=202)
def create_diagnosis(body: AnomalyEventRequest, background_tasks: BackgroundTasks):
    diagnosis_id = body.event_id  # 复用 Java 生成的 eventId，天然幂等
    with _LOCK:
        existing = _JOBS.get(diagnosis_id)
        if existing is not None:
            return DiagnosisJobResponse(
                diagnosis_id=diagnosis_id, event_id=body.event_id, status=existing["status"]
            )
        _JOBS[diagnosis_id] = {
            "status": "pending", "event_id": body.event_id, "final": None, "error": None,
        }
    background_tasks.add_task(_run_diagnosis, diagnosis_id, body)
    return DiagnosisJobResponse(diagnosis_id=diagnosis_id, event_id=body.event_id, status="pending")


@app.get("/api/v1/diagnoses/{diagnosis_id}", response_model=DiagnosisResultResponse)
def get_diagnosis(diagnosis_id: str):
    with _LOCK:
        job = _JOBS.get(diagnosis_id)
    if job is None:
        raise HTTPException(status_code=404, detail="diagnosis not found")

    final = job.get("final") or {}
    return DiagnosisResultResponse(
        diagnosis_id=diagnosis_id,
        event_id=job["event_id"],
        status=job["status"],
        error=job.get("error"),
        conclusion_id=final.get("conclusion_id"),
        conclusion=final.get("conclusion"),
        confidence=final.get("confidence"),
        low_confidence=final.get("low_confidence"),
        anomaly_type=final.get("anomaly_type"),
        direction=final.get("direction"),
        specialized=final.get("specialized"),
        note=final.get("note"),
        converged=final.get("converged"),
        tool_calls_used=final.get("tool_calls_used"),
        budget=final.get("budget"),
        ranking=final.get("ranking"),
        curation=final.get("curation"),
    )
