"""agent/llm_client.py 的两组独立测试：

1. _mock_judgment 的 r5/r6/r8 分支必须依据 tool_result 实际内容判断，
   不能是无视输入的硬编码常量（r6/r8 修复的回归测试，r5 是既有正确行为的锁定）。
2. 不受信任内容隔离：web_search 的结果是外部输入，可能藏 prompt injection，
   传给 LLM 判断前要用 <SEARCH_RESULTS> 标签包起来，跟其他工具的结果区分开。
"""
from agent.llm_client import (
    _mock_judgment,
    _wrap_tool_result_for_prompt,
    llm_structured_judgment,
)


def _hits(*phenomena):
    return {"hits": [{"phenomenon": p} for p in phenomena]}


def test_r6_seasonality_triggers_on_全渠道整体性_pattern():
    question = ("检索到的历史案例中，是否有证据表明当前时间段（7月下旬）历史上存在"
                "规律性、全渠道整体性的逾期抬升？注意区分'单一渠道异常'和'全渠道整体抬升'。")

    hit = _mock_judgment(question, _hits("全渠道逾期率在春节后两周整体抬升，无单一渠道异常"))
    assert hit["triggered"] is True

    miss = _mock_judgment(question, _hits("某新接入渠道占比上升，逾期率显著高于其他渠道"))
    assert miss["triggered"] is False


def test_r8_data_quality_triggers_on_etl_pattern():
    question = ("根据知识库中的口径说明，当前'逾期率持续多日偏高（而非单日跳变后恢复）'的形态，"
                "是否符合历史上ETL任务失败导致的数据故障特征？")

    hit = _mock_judgment(question, _hits("历史上出现过ETL任务失败导致分母缺失、比率虚高的情况"))
    assert hit["triggered"] is True

    miss = _mock_judgment(question, _hits("审批线变更需风控委员会审批并留存变更记录"))
    assert miss["triggered"] is False


def test_r5_approval_line_no_change_still_reads_hits_text():
    question = "根据知识库内容，审批线在最近三个月内是否【没有】发生过变更？（没有变更=triggered为true，作为对'审批口径变化'假设的反证）"

    no_change = _mock_judgment(question, _hits("当前审批线为580分，最近一次调整时间为2025年3月"))
    assert no_change["triggered"] is True

    changed = _mock_judgment(question, _hits("审批线于2026年7月18日下调至560分"))
    assert changed["triggered"] is False


def test_wrap_web_search_result_adds_search_results_tags():
    tool_result = {"tool_name": "web_search", "query": "q", "hits": [{"title": "t"}]}
    wrapped = _wrap_tool_result_for_prompt(tool_result)
    assert wrapped.startswith("<SEARCH_RESULTS>\n")
    assert wrapped.endswith("\n</SEARCH_RESULTS>")
    assert '"title": "t"' in wrapped


def test_wrap_non_search_result_has_no_tags():
    tool_result = {"tool_name": "query_dashboard", "summary": {}, "records": []}
    wrapped = _wrap_tool_result_for_prompt(tool_result)
    assert "<SEARCH_RESULTS>" not in wrapped


def test_mock_judgment_regulatory_question_defaults_to_not_triggered():
    tool_result = {
        "tool_name": "web_search",
        "hits": [
            {"title": "监管部门发布小额贷款业务合规指引（例行更新）",
             "snippet": "重申现有信息披露和利率上限要求，未新增审批或准入限制条款，属例行性文件更新。"},
        ],
    }
    question = "网络检索到的信息中，是否有可信来源明确指出近期信贷/助贷行业监管政策发生了收紧或重大调整？"
    result = llm_structured_judgment(question, tool_result)
    assert result["triggered"] is False


def test_mock_judgment_regulatory_question_triggers_on_explicit_tightening_signal():
    tool_result = {
        "tool_name": "web_search",
        "hits": [{"title": "监管新规", "snippet": "监管部门宣布对助贷行业实施全面收紧措施。"}],
    }
    question = "网络检索到的信息中，是否有可信来源明确指出近期信贷/助贷行业监管政策发生了收紧或重大调整？"
    result = llm_structured_judgment(question, tool_result)
    assert result["triggered"] is True
