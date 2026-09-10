"""归因假设池 + 证据规则表 + 树节点定义。

设计对应打分卡思路：每条证据按 weight 累加到假设分数上；
llm_judgment 类规则的贡献 = weight * confidence（连续缩放，不是二元）。
"""
from dataclasses import dataclass, field


# ── 树节点 ────────────────────────────────────────────
@dataclass
class ProbeNode:
    """假设树的叶子：一个待验证的具体子问题，对应一次工具调用。"""
    node_id: str
    hypothesis: str          # 归属的假设
    question: str            # 要验证什么
    tool_name: str           # query_dashboard / retrieve_historical_case / retrieve_business_kb
    tool_args: dict
    status: str = "pending"  # pending / done
    result_digest: str = ""  # 折叠后的摘要（cache breakpoint 思路）


@dataclass
class Hypothesis:
    hyp_id: str
    description: str
    score: float = 0.0
    evidence_log: list = field(default_factory=list)
    probes: list = field(default_factory=list)  # list[ProbeNode]


def build_hypothesis_tree(
    metric_name: str = "fpd30_rate",
    baseline_range: str = "2026-06-01~2026-07-19",
    anomaly_range: str = "2026-07-20~2026-07-30",
    phenomenon: str = "新增用户FPD30逾期率近期环比明显上升",
) -> dict[str, Hypothesis]:
    """初始化四个假设及其探测子节点（树的第一层展开）。
    时间范围由 Orchestrator.run() 动态传入，不再写死。

    p_ch_case/p_mc_case/p_dq_kb 三个检索类 probe 的 query_text 用 phenomenon
    动态拼，不再写死固定短语——写死的版本不管真实场景数据是什么，永远查同一句话、
    命中同一条 KB/历史案例条目，等于送分而不是真检验（scripts/eval_scenarios.py
    2026-08-16 实测过：channel_quality_decline 的 r3 在 approval_policy_change
    场景里照样以 0.85 置信度触发，纯噪声）。跟 Step 0 的 _seed_prior() 用
    phenomenon 做先验检索是同一个思路，不再各写各的固定短语。
    """
    full_range = f"{baseline_range.split('~')[0]}~{anomaly_range.split('~')[1]}"
    tree = {
        "channel_quality_decline": Hypothesis(
            "channel_quality_decline", "获客渠道质量下滑，新增用户本身风险更高",
            probes=[
                ProbeNode("p_ch_share", "channel_quality_decline",
                          "各渠道放款占比近期是否发生结构性变化",
                          "query_dashboard",
                          {"metric_name": "channel_share", "date_range": full_range,
                           "dimensions": ["channel"]}),
                ProbeNode("p_ch_overdue", "channel_quality_decline",
                          "分渠道看逾期率，是否某渠道显著高于大盘",
                          "query_dashboard",
                          {"metric_name": metric_name, "date_range": anomaly_range, "dimensions": ["channel"]}),
                ProbeNode("p_ch_case", "channel_quality_decline",
                          "历史上是否有渠道放量导致逾期抬升的先例",
                          "retrieve_historical_case",
                          {"query_text": phenomenon}),
            ]),
        "approval_policy_change": Hypothesis(
            "approval_policy_change", "审批口径变化，放款给了原本会被拒的用户",
            probes=[
                ProbeNode("p_ap_score", "approval_policy_change",
                          "放款用户平均信用分是否下移（整体口径）",
                          "query_dashboard",
                          {"metric_name": "avg_credit_score",
                           "date_range": full_range}),
                ProbeNode("p_ap_kb", "approval_policy_change",
                          "审批线近期是否有变更记录",
                          "retrieve_business_kb",
                          {"query_text": "审批线 变更 调整 时间"}),
            ]),
        "macro_seasonality": Hypothesis(
            "macro_seasonality", "宏观周期性因素（季节性还款压力等）",
            probes=[
                ProbeNode("p_mc_case", "macro_seasonality",
                          "历史同期是否出现过类似的整体性波动",
                          "retrieve_historical_case",
                          {"query_text": phenomenon}),
            ]),
        "data_quality_issue": Hypothesis(
            "data_quality_issue", "数据本身异常（口径变化、统计错误）",
            probes=[
                ProbeNode("p_dq_count", "data_quality_issue",
                          "放款笔数（分母）是否有异常跳变",
                          "query_dashboard",
                          {"metric_name": "loan_count", "date_range": anomaly_range}),
                ProbeNode("p_dq_kb", "data_quality_issue",
                          "统计口径说明及历史数据故障情况",
                          "retrieve_business_kb",
                          {"query_text": "逾期率 统计口径 数据 ETL 任务失败"}),
            ]),
        "regulatory_policy_change": Hypothesis(
            "regulatory_policy_change", "外部监管/行业政策收紧，影响放款资质或还款能力",
            probes=[
                ProbeNode("p_reg_search", "regulatory_policy_change",
                          "近期是否有信贷/助贷行业监管政策收紧或重大调整",
                          "web_search",
                          {"query": f"信贷 助贷行业 监管政策 {anomaly_range.split('~')[0][:7]} 收紧 调整"}),
                ProbeNode("p_reg_kb", "regulatory_policy_change",
                          "业务知识库里是否也有相应政策变更的记录",
                          "retrieve_business_kb",
                          {"query_text": "监管政策 合规要求 变更"}),
            ]),
    }
    return tree


# ── 展示标签 ─────────────────────────────────────────
# 跟假设树的存在维护在同一个文件里，避免 app.py 之类的下游消费者
# 各自手抄一份、加新假设时忘记同步（历史上发生过：app.py 曾经漏掉
# regulatory_policy_change，导致该假设胜出时直接 KeyError 崩溃）。
HYP_LABELS = {
    "channel_quality_decline": "渠道质量下滑",
    "approval_policy_change": "审批口径变化",
    "macro_seasonality": "宏观季节性",
    "data_quality_issue": "数据质量问题",
    "regulatory_policy_change": "外部监管/行业政策",
    "other_unclassified": "未归类（证据不足）",
}


# ── 证据规则表 ─────────────────────────────────────────
# numeric: check(tool_result) -> bool，纯代码判断
# llm_judgment: 给 LLM 的判断问题，返回 {triggered, confidence, reasoning}
EVIDENCE_RULES = [
    # —— 渠道质量下滑 ——
    {
        "rule_id": "r1", "hypothesis": "channel_quality_decline", "weight": 0.30,
        "type": "numeric", "probe_id": "p_ch_share",
        "desc": "某渠道占比在异常期环比上升超过15个百分点",
        "check": "channel_share_shift",
    },
    {
        "rule_id": "r2", "hypothesis": "channel_quality_decline", "weight": 0.30,
        "type": "numeric", "probe_id": "p_ch_overdue",
        "desc": "某渠道逾期率高出大盘均值50%以上",
        "check": "channel_overdue_gap",
    },
    {
        "rule_id": "r3", "hypothesis": "channel_quality_decline", "weight": 0.20,
        "type": "llm_judgment", "probe_id": "p_ch_case",
        "desc": "历史案例中存在高度相似的渠道放量致逾期案例",
        "question": "检索到的历史案例中，是否存在与'渠道放量+该渠道用户资质偏弱导致逾期率上升'高度相似的案例？",
    },
    # —— 审批口径变化 ——
    {
        "rule_id": "r4", "hypothesis": "approval_policy_change", "weight": 0.35,
        "type": "numeric", "probe_id": "p_ap_score",
        "desc": "放款用户平均信用分异常期较基线期下降超过10分",
        "check": "score_drop",
    },
    {
        # 缺席证据（"没查到变更记录"）本身就该比 r4 那种直接测量到的数值信号弱，
        # 权重故意明显小于 r4（0.35），避免一条弱反证打平一条强正证据——
        # 实测过 approval_policy_change 场景：r4 稳定触发（信用分实测降23分，
        # 远超10分门槛），旧权重 -0.30 几乎能完全抵消 r4 的 +0.35，导致该假设
        # 置信度长期卡在置信度地板（config.MIN_CONFIDENCE_FLOOR）以下，
        # 3/3 场景全部错误归入 other_unclassified（scripts/eval_scenarios.py 实测）。
        "rule_id": "r5", "hypothesis": "approval_policy_change", "weight": -0.10,
        "type": "llm_judgment", "probe_id": "p_ap_kb",
        "desc": "（反证）KB显示审批线近期无变更记录",
        "question": "根据知识库内容，审批线在最近三个月内是否【没有】发生过变更？（没有变更=triggered为true，作为对'审批口径变化'假设的反证）",
    },
    # —— 宏观季节性 ——
    {
        "rule_id": "r6", "hypothesis": "macro_seasonality", "weight": 0.35,
        "type": "llm_judgment", "probe_id": "p_mc_case",
        "desc": "历史案例显示同期存在规律性的整体抬升",
        "question": "检索到的历史案例中，是否有证据表明当前时间段（7月下旬）历史上存在规律性、全渠道整体性的逾期抬升？注意区分'单一渠道异常'和'全渠道整体抬升'。",
    },
    # —— 数据质量 ——
    {
        "rule_id": "r7", "hypothesis": "data_quality_issue", "weight": 0.40,
        "type": "numeric", "probe_id": "p_dq_count",
        "desc": "放款笔数出现单日跳变（超过均值±40%）",
        "check": "count_jump",
    },
    {
        "rule_id": "r8", "hypothesis": "data_quality_issue", "weight": 0.15,
        "type": "llm_judgment", "probe_id": "p_dq_kb",
        "desc": "口径说明提示当前场景符合历史数据故障特征",
        "question": "根据知识库中的口径说明，当前'逾期率持续多日偏高（而非单日跳变后恢复）'的形态，是否符合历史上ETL任务失败导致的数据故障特征？",
    },
    # —— 外部监管/行业政策 ——
    {
        "rule_id": "r9", "hypothesis": "regulatory_policy_change", "weight": 0.40,
        "type": "llm_judgment", "probe_id": "p_reg_search",
        "desc": "网络检索到可信来源证实近期行业监管/政策发生显著收紧或调整",
        "question": "网络检索到的信息中，是否有可信来源明确指出近期信贷/助贷行业监管政策发生了收紧或重大调整？",
    },
    {
        "rule_id": "r10", "hypothesis": "regulatory_policy_change", "weight": 0.20,
        "type": "llm_judgment", "probe_id": "p_reg_kb",
        "desc": "业务知识库中也有相应政策变更记录佐证",
        "question": "业务知识库中是否也记录了相应的监管政策变更信息，与网络检索结果相互印证？",
    },
]
