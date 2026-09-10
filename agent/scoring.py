"""评分引擎：check_condition 混合"代码判断"和"LLM判断"。

numeric 规则  -> 纯 pandas/python 计算，确定且免费
llm_judgment -> LLM 返回 {triggered, confidence, reasoning}，贡献 = weight * confidence
"""
import pandas as pd
from agent.llm_client import llm_structured_judgment


# ── numeric 检查函数注册表 ─────────────────────────────
# split_date: 基线期/异常期的分界点（= anomaly_range 起始日），由 Orchestrator 动态传入，
# 不再写死，否则 Java 传入不同的 anomaly_range 时打分逻辑仍按旧日期分割。
def _channel_share_shift(result: dict, split_date: str) -> tuple[bool, str]:
    df = pd.DataFrame(result["records"])
    df["period"] = df["date"].apply(lambda d: "recent" if d >= split_date else "base")
    pivot = df.groupby(["period", "channel"])["value"].mean().unstack(fill_value=0)
    if "recent" not in pivot.index or "base" not in pivot.index:
        return False, "数据不足以对比两期"
    shift = (pivot.loc["recent"] - pivot.loc["base"])
    top = shift.idxmax()
    return bool(shift.max() > 0.15), f"{top} 占比变化 {shift.max():+.1%}（基线 {pivot.loc['base', top]:.1%} → 近期 {pivot.loc['recent', top]:.1%}）"


def _channel_overdue_gap(result: dict, split_date: str) -> tuple[bool, str]:
    df = pd.DataFrame(result["records"])
    by_ch = df.groupby("channel")["value"].mean()
    top = by_ch.idxmax()
    others = by_ch.drop(top).mean()  # 对比"其他渠道均值"，避免异常渠道自己稀释大盘
    gap = by_ch.max() / others - 1
    return bool(gap > 0.5), f"{top} 逾期率 {by_ch.max():.2%}，高出其他渠道均值（{others:.2%}）{gap:+.0%}"


def _score_drop(result: dict, split_date: str) -> tuple[bool, str]:
    df = pd.DataFrame(result["records"])
    base = df[df["date"] < split_date]["value"].mean()
    recent = df[df["date"] >= split_date]["value"].mean()
    drop = base - recent
    return bool(drop > 10), f"平均信用分 基线 {base:.0f} → 近期 {recent:.0f}（下降 {drop:.0f} 分）"


def _count_jump(result: dict, split_date: str) -> tuple[bool, str]:
    df = pd.DataFrame(result["records"])
    mean = df["value"].mean()
    dev = (df["value"] / mean - 1).abs().max()
    return bool(dev > 0.4), f"放款笔数最大单日偏离均值 {dev:.0%}，{'存在' if dev > 0.4 else '未见'}异常跳变"


NUMERIC_CHECKS = {
    "channel_share_shift": _channel_share_shift,
    "channel_overdue_gap": _channel_overdue_gap,
    "score_drop": _score_drop,
    "count_jump": _count_jump,
}


def apply_rules(probe_id: str, tool_result: dict, rules: list, tree: dict, split_date: str) -> list[dict]:
    """一次工具调用返回后，触发所有绑定在该 probe 上的规则，更新假设分数。
    split_date: 基线期/异常期分界点（= anomaly_range 起始日），动态传入。
    返回本次产生的证据条目列表（供 UI 展示）。"""
    fired = []
    for rule in rules:
        if rule["probe_id"] != probe_id:
            continue
        hyp = tree[rule["hypothesis"]]

        if "error" in tool_result:
            # 工具调用本身失败（如 web_search 超时/网络异常）时，不能让 llm_judgment
            # 规则把"没查到"误判成"查了但没发现"——两者在证据日志里必须能区分开。
            continue

        if rule["type"] == "numeric":
            triggered, detail = NUMERIC_CHECKS[rule["check"]](tool_result, split_date)
            confidence = 1.0
        else:
            judgment = llm_structured_judgment(rule["question"], tool_result)
            triggered = judgment["triggered"]
            confidence = judgment["confidence"]
            detail = judgment["reasoning"]

        if triggered:
            contribution = round(rule["weight"] * confidence, 3)
            hyp.score = round(hyp.score + contribution, 3)
            entry = {
                "rule_id": rule["rule_id"],
                "hypothesis": rule["hypothesis"],
                "desc": rule["desc"],
                "detail": detail,
                "weight": rule["weight"],
                "confidence": confidence,
                "contribution": contribution,
                "source_probe": probe_id,
                "judge_type": rule["type"],
            }
            hyp.evidence_log.append(entry)
            fired.append(entry)
    return fired
