"""Streamlit 前端：展示排查全过程（推理过程可视化是核心卖点）。

运行: streamlit run app.py
"""
from datetime import date

import pandas as pd
import streamlit as st

import config
from tools.dashboard import query_dashboard
from agent.orchestrator import Orchestrator
from agent.hypotheses import HYP_LABELS
from data.scenario_runtime import use_scenario_data

st.set_page_config(page_title="信贷数据异常排查 Agent", layout="wide")
st.title("信贷数据异常排查 Agent")
st.caption(f"模式: {'MOCK（无需API key）' if config.MOCK_LLM else f'Kimi {config.LLM_MODEL}'} ｜ "
           f"查询预算: {config.MAX_TOOL_CALLS} 次 ｜ 收敛阈值: 领先 {config.CONVERGE_MARGIN} 分")


# ── 共享渲染逻辑：两个 tab 都用同一份 ────────────────────
def render_trace(trace: list[dict]) -> None:
    for step in trace:
        if step["step_type"] == "think":
            st.info(f"🧠 **Think** (round {step['round']})　{step['text']}")
        elif step["step_type"] == "reflect":
            st.warning(f"🔍 **Reflect 自我核验**　{step['text']}")
        else:
            tag = "🔁 [核验补查] " if step.get("is_reflect") else ""
            with st.expander(
                f"🛠 {tag}{HYP_LABELS.get(step['hypothesis'], step['hypothesis'])} ｜ {step['question']} "
                f"→ `{step['tool_name']}`" + ("　*(已折叠)*" if step.get("folded") else "")
            ):
                st.code(str(step["tool_args"]), language="python")
                st.write("**摘要**:", step["digest"])
                if not step.get("folded") and "raw_result" in step:
                    st.json(step["raw_result"], expanded=False)
                for ev in step["evidence_fired"]:
                    sign = "＋" if ev["contribution"] >= 0 else "－"
                    st.success(
                        f"{sign} 证据触发 [{ev['rule_id']}] {ev['desc']}\n\n"
                        f"　详情: {ev['detail']}\n\n"
                        f"　贡献 = 权重 {ev['weight']} × 置信度 {ev['confidence']:.2f} "
                        f"= **{ev['contribution']:+.3f}** → {HYP_LABELS.get(ev['hypothesis'], ev['hypothesis'])}"
                        + (f"　*(LLM判断)*" if ev["judge_type"] == "llm_judgment" else "　*(规则判断)*")
                    )


def render_conclusion(final: dict) -> None:
    st.header("③ 归因结论")
    rank_df = pd.DataFrame([
        {"假设": HYP_LABELS.get(h["hyp_id"], h["hyp_id"]), "置信度": h["score"]} for h in final["ranking"]
    ]).set_index("假设")
    st.bar_chart(rank_df, height=220, horizontal=True)

    st.markdown(
        f"### 最终归因：**{HYP_LABELS.get(final['conclusion_id'], final['conclusion_id'])}**（置信度 {final['confidence']:.2f}）\n"
        f"- 收敛状态: {'✅ 提前收敛' if final['converged'] else '⏱ 预算耗尽后取最高分'}\n"
        f"- 查询预算使用: {final['tool_calls_used']} / {final['budget']}\n"
        + (f"- 次优假设: {final['runner_up']['desc']}（{final['runner_up']['score']:.2f}）"
           if final["runner_up"] else "")
    )

    with st.expander("完整证据链（evidence_log）"):
        for h in final["ranking"]:
            if h["evidence"]:
                st.markdown(f"**{HYP_LABELS.get(h['hyp_id'], h['hyp_id'])}** — 总分 {h['score']:.3f}")
                st.table(pd.DataFrame(h["evidence"])[
                    ["rule_id", "desc", "detail", "weight", "confidence", "contribution"]])


tab_fixed, tab_custom = st.tabs(["固定演示", "自定义异常输入"])

# ── Tab 1: 固定演示（行为与改动前完全一致） ───────────────
with tab_fixed:
    st.header("① 异常现象")
    overview = query_dashboard("fpd30_rate", "2026-06-01~2026-07-30")
    if "records" not in overview:
        st.warning("数据源暂时不可用（另一会话正在运行自定义排查），请稍后刷新")
    else:
        df = pd.DataFrame(overview["records"])
        base = df[df["date"] < "2026-07-20"]["value"].mean()
        recent = df[df["date"] >= "2026-07-20"]["value"].mean()

        c1, c2, c3 = st.columns(3)
        c1.metric("基线期 FPD30", f"{base:.2%}")
        c2.metric("异常期 FPD30", f"{recent:.2%}", delta=f"{(recent/base-1):+.0%}", delta_color="inverse")
        c3.metric("异常期起点", "2026-07-20")
        st.line_chart(df.set_index("date")["value"], height=220)

    st.header("② Agent 排查过程")
    if st.button("开始排查", type="primary", key="fixed_run"):
        with st.spinner("Agent 排查中…"):
            orch = Orchestrator()
            final = orch.run()
        st.session_state["trace"] = orch.trace
        st.session_state["final"] = final

    if "trace" in st.session_state:
        render_trace(st.session_state["trace"])
        render_conclusion(st.session_state["final"])

# ── Tab 2: 自定义异常输入（Task 4 补齐表单和排查逻辑） ─────
def _fmt_date_range(date_pair: tuple) -> str:
    start, end = date_pair
    return f"{start.isoformat()}~{end.isoformat()}"


with tab_custom:
    st.info(
        "演示模式未接入公司数据仓库，看板类证据（渠道占比 / 分渠道逾期率 / "
        "平均信用分 / 放款笔数）暂不可用，归因结论目前只基于知识库检索和网络检索——"
        "接入真实数据源后可补齐。因此本模式下总置信度会明显偏低，多数情况会落到"
        "「未归类（证据不足）」，这是数据源缺失的预期结果，不是排查失败。"
    )

    with st.form("custom_anomaly_form"):
        phenomenon = st.text_area("异常描述", placeholder="如：新增用户FPD30逾期率近期环比明显上升")
        metric_name = st.text_input("指标名称", value="fpd30_rate")
        baseline_range = st.date_input("基线期", value=(date(2026, 6, 1), date(2026, 7, 19)))
        anomaly_range = st.date_input("异常期", value=(date(2026, 7, 20), date(2026, 7, 30)))
        baseline_value = st.number_input("基线期水平", format="%.4f", min_value=0.0001, value=0.03)
        anomaly_value = st.number_input("异常期水平", format="%.4f", min_value=0.0, value=0.05)
        submitted = st.form_submit_button("开始排查", type="primary")

    if submitted:
        if not phenomenon.strip():
            st.error("请填写异常描述")
        elif len(baseline_range) != 2 or len(anomaly_range) != 2:
            st.error("请选择完整的日期范围（起止两个日期）")
        else:
            c1, c2 = st.columns(2)
            c1.metric("基线期水平", f"{baseline_value:.2%}")
            c2.metric("异常期水平", f"{anomaly_value:.2%}",
                       delta=f"{(anomaly_value/baseline_value-1):+.0%}", delta_color="inverse")
            direction = "UP" if anomaly_value > baseline_value else "DOWN"
            with st.spinner("Agent 排查中…"):
                with use_scenario_data(None):
                    orch = Orchestrator()
                    final = orch.run(
                        phenomenon=phenomenon,
                        metric_name=metric_name,
                        baseline_range=_fmt_date_range(baseline_range),
                        anomaly_range=_fmt_date_range(anomaly_range),
                        direction=direction,
                    )
            st.session_state["custom_trace"] = orch.trace
            st.session_state["custom_final"] = final

    if "custom_trace" in st.session_state:
        st.header("② Agent 排查过程")
        render_trace(st.session_state["custom_trace"])
        render_conclusion(st.session_state["custom_final"])
