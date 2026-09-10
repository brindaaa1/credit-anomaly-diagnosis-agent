"""批量跑 Orchestrator 对比多场景真值，产出准确率/置信度/收敛情况汇总。
手动运行的调优工具，不接入 CI。

用法：
    cd credit-anomaly-agent
    PYTHONPATH=. python scripts/eval_scenarios.py                              # 跑全部16个场景
    PYTHONPATH=. python scripts/eval_scenarios.py --scenario macro_seasonality # 只跑某一类
    PYTHONPATH=. python scripts/eval_scenarios.py --out eval_results          # 指定输出目录（默认值）

读结果时的注意事项：`macro_seasonality`（规则 r6）和 `data_quality_issue`（规则 r8）
这两条 llm_judgment 规则，在 MOCK_LLM 下判断的是固定 query 检索回来的 KB 文本，而不是
本次生成的场景数据，贡献的不随场景数据变化，是 KB 静态、probe query 不随场景生成而
带来的先验加分（`docs/superpowers/specs/2026-08-05-scenario-eval-harness-design.md`
"已知限制"一节已提到 KB 不做场景化）。`p_mc_case` 的 query_text 写死为"季节性 整体抬升
全渠道 同期"，在 `kb/historical_cases.json` 里恒定命中同一条 `case_2025_08`（同时含
"全渠道"和"整体"），r6 因此恒为 triggered=True；`p_dq_kb` 的 query_text 写死为"逾期率
统计口径 数据 ETL 任务失败"，在 `kb/business_kb.md` 里恒定命中"数据口径"词条（同时含
"ETL"/"分母"/"当日"），r8 同理恒为 triggered=True。2026-08-16 实测确认过：这两条改成
phenomenon 驱动检索并不能真正解决问题——`retrieve_business_kb` 对任何场景的 phenomenon
文本相似度都落在 0.65 附近的窄区间（business_kb 是通用术语表，没有具体情节可比对），
`retrieve_historical_case` 虽然区分度够，但 macro_seasonality 自己的 phenomenon 检索出的
top-1 经常是 channel_quality_decline 而不是自己（两类场景描述文本本身区分度不够），收紧
判断逻辑反而会让它连自己的真阳性场景都测不出来（详见 agent/llm_client.py::_mock_judgment
里 "季节" 分支的注释）。r6/r8 因此保留原状，留作已知限制。

跟 r6/r8 同类型但已修复的是 `channel_quality_decline`（规则 r3）：原来 `p_ch_case` 的
query_text 也是写死的（"逾期率上升 渠道占比变化 新渠道放量"），且 `_mock_judgment` 当时是在
整个 top-k 检索结果里搜子串、不看排名，两者叠加导致 r3 在完全没有渠道信号的场景里也会
以 0.85 置信度触发（`approval_policy_change_s42` 实测过，见
tests/test_approval_policy_regression.py）。已改成 phenomenon 驱动检索
+ `_mock_judgment` 只看 top-1 命中，r3 现在会正确响应场景数据。
实测证据：`ambiguous_negative` 场景在数据上完全没有结构性信号，MIN_CONFIDENCE_FLOOR（见
config.py，当前 0.30）引入前会被包装成 `macro_seasonality` 结论，置信度 0.274——跟三个
真正的 `macro_seasonality` 场景（置信度 0.311）几乎没有区分度。引入置信度地板后，
`_finalize()` 在最高分低于地板时返回 `other_unclassified`，`ambiguous_negative` 现在
正确报告为 other_unclassified（置信度数值不变，只是不再包装成一个看似确定的已知归因）。
"macro_seasonality" 3/3 全对的准确率数字本身没有变化（`ambiguous_negative` 的 ground_truth
本来就是 None，不参与准确率计算），但决策呈现层面的误导已经修正。
`approval_policy_change_s42/s101/s202` 三个场景历史上也受过置信度地板影响，但根因在
2026-08-16 已经修复，不再是"靠地板兜底成诚实的 other_unclassified"，而是能正确归因了：
r5（"KB无变更记录"反证，旧权重 -0.30）当时几乎必然触发，抵消掉 r4（信用分实测下降23分，
权重 +0.35）的正确信号，导致该假设分数长期卡在地板以下（当时实测 0.192，比错误归因
`channel_quality_decline` 还低）。r5 权重调到 -0.10、r3 的静态检索噪声也一并修复后，
现在这三个场景稳定输出正确结论，置信度 0.342（回归测试见
tests/test_approval_policy_regression.py）。截至 2026-08-16，整体准确率 15/15（100%）。
"""
import argparse
import json
import os
from datetime import datetime, timezone

from agent.orchestrator import Orchestrator
from data.scenario_runtime import use_scenario_data
from data.scenarios import SCENARIOS, iter_runs


def run_one(run_id: str, df, spec) -> dict:
    """把场景 DataFrame 喂给 Orchestrator，对比预测结论与场景真值。
    数据注入的临时文件/缓存管理由 use_scenario_data() 负责，见 data/scenario_runtime.py。"""
    with use_scenario_data(df):
        try:
            final = Orchestrator().run(phenomenon=spec.phenomenon)
        except Exception as exc:
            return {
                "run_id": run_id, "scenario_type": spec.scenario_type,
                "ground_truth": spec.ground_truth, "error": f"{type(exc).__name__}: {exc}",
            }
        predicted = final["conclusion_id"]
        hit = (predicted == spec.ground_truth) if spec.ground_truth else None
        runner_up_gap = (
            round(final["confidence"] - final["runner_up"]["score"], 3)
            if final.get("runner_up") else round(final["confidence"], 3)
        )
        return {
            "run_id": run_id,
            "scenario_type": spec.scenario_type,
            "ground_truth": spec.ground_truth,
            "predicted": predicted,
            "hit": hit,
            "confidence": final["confidence"],
            "runner_up_gap": runner_up_gap,
            "converged": final["converged"],
            "tool_calls_used": final["tool_calls_used"],
            "ranking": final["ranking"],
        }


def _print_table(results: list[dict]) -> None:
    header = f"{'场景':<32}{'真值':<26}{'预测':<26}{'命中':<6}{'置信度':<8}{'次优分差':<10}{'工具调用':<8}"
    print(header)
    print("-" * len(header))
    scored = [r for r in results if r.get("ground_truth")]
    hits = 0
    for r in results:
        if "error" in r:
            print(f"{r['run_id']:<32}{'ERROR':<26}{r['error'][:60]}")
            continue
        hit_mark = "n/a" if r["hit"] is None else ("✓" if r["hit"] else "✗")
        if r["hit"]:
            hits += 1
        print(f"{r['run_id']:<32}{str(r['ground_truth']):<26}{r['predicted']:<26}"
              f"{hit_mark:<6}{r['confidence']:<8.3f}{r['runner_up_gap']:<10.3f}{r['tool_calls_used']:<8}")
    if scored:
        print(f"\n准确率: {hits}/{len(scored)} ({hits / len(scored):.1%})")


def main(scenario_filter: str | None, out_dir: str) -> list[dict]:
    results = []
    for run_id, df, spec in iter_runs():
        if scenario_filter and spec.scenario_type != scenario_filter:
            continue
        results.append(run_one(run_id, df, spec))

    _print_table(results)

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(out_dir, f"{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整结果已写入 {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default=None,
                         help="只跑指定类型（不传则跑全部场景）")
    parser.add_argument("--out", default="eval_results", help="JSON 结果输出目录")
    args = parser.parse_args()
    main(args.scenario, args.out)
