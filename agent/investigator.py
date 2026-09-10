"""Investigator：被 Orchestrator 派发单个 ProbeNode，执行工具调用，
返回紧凑的结构化结果（不是长篇分析）。

职责单一：调工具 -> 打包结果。判断和评分留给 scoring 层，
决策留给 Orchestrator —— 对应之前讨论的角色划分。
"""
import json
from tools.dashboard import query_dashboard
from tools.retrieval import retrieve_historical_case, retrieve_business_kb
from tools.web_search import web_search
from agent.llm_client import llm_summarize, _wrap_tool_result_for_prompt
from agent.hypotheses import ProbeNode

TOOL_REGISTRY = {
    "query_dashboard": query_dashboard,
    "retrieve_historical_case": retrieve_historical_case,
    "retrieve_business_kb": retrieve_business_kb,
    "web_search": web_search,
}


def run_probe(probe: ProbeNode) -> dict:
    """执行一个探测节点。返回 {probe, raw_result, digest}。"""
    fn = TOOL_REGISTRY[probe.tool_name]
    raw = fn(**probe.tool_args)

    # 折叠成摘要：这就是主 loop 里"边界外内容压缩"的落点，
    # 老的 probe 只在 context 里保留 digest，raw 只有最近 K 个保留
    # digest 也会进 trace 给人看。web_search 结果是不受信任的外部内容，
    # 用 <SEARCH_RESULTS> 隔离防止提示词注入；其他工具结果是本地数据，
    # 仅选择 summary 或 hits（如果存在）来保持原始内容压缩策略。
    if raw.get("tool_name") == "web_search":
        digest_src = _wrap_tool_result_for_prompt(raw)
    else:
        digest_src = json.dumps(raw.get("summary") or raw.get("hits") or raw, ensure_ascii=False, default=str)
    probe.result_digest = llm_summarize(f"[{probe.question}] {digest_src}")
    probe.status = "done"

    return {"probe": probe, "raw_result": raw, "digest": probe.result_digest}
