"""Orchestrator：主循环。

best-first 树搜索：
  每轮取当前分数最高（并列时按剩余 probe 多者优先）的假设，
  展开其 pending 的 probe（每轮最多 MAX_EXPAND_PER_ROUND 个），
  派发给 Investigator -> 结果交给 scoring 更新分数 ->
  检查收敛（领先幅度 > CONVERGE_MARGIN 或预算耗尽）。

收敛后走一轮 Reflect 自我核验（巧思3）：
  找最终结论中最薄弱的证据，若对立假设还有未查的 probe，补查一次再定案。

context 折叠（cache breakpoint 思路）：
  trace 中只有最近 CONTEXT_WINDOW_K 次工具调用保留 raw_result，
  更早的只保留 digest —— 对应"四段边界外内容压成摘要"。
"""
from dataclasses import asdict
import config
from agent.hypotheses import build_hypothesis_tree, EVIDENCE_RULES
from agent.investigator import run_probe
from agent.scoring import apply_rules


class Orchestrator:
    def __init__(self):
        self.tree = build_hypothesis_tree()
        self.trace: list[dict] = []       # 完整推理轨迹（UI 展示用）
        self.tool_calls = 0
        self.converged = False
        self.final = None

    # ── context 折叠 ─────────────────────────────────
    def _fold_context(self):
        """最近 K 次保留 raw，更早的删掉 raw 只留 digest。"""
        probe_steps = [s for s in self.trace if s["step_type"] == "probe"]
        for s in probe_steps[:-config.CONTEXT_WINDOW_K]:
            s.pop("raw_result", None)
            s["folded"] = True

    # ── 选择策略 ─────────────────────────────────────
    def _pick_hypothesis(self):
        cands = [h for h in self.tree.values()
                 if any(p.status == "pending" for p in h.probes)]
        if not cands:
            return None
        return max(cands, key=lambda h: (h.score, sum(p.status == "pending" for p in h.probes)))

    def _ranked(self):
        return sorted(self.tree.values(), key=lambda h: -h.score)

    def _check_converged(self) -> bool:
        r = self._ranked()
        if len(r) < 2:
            return True
        return (r[0].score - r[1].score) > config.CONVERGE_MARGIN

    # ── Step 0a: Java 可疑细分先验注入 ──────────────────
    def _inject_suspect_segments(self, suspect_segments: list):
        """Java 已预先定位出可疑细分（如 channel_C 贡献度 72%），
        直接按贡献度给 channel_quality_decline 注入先验分，跳过对应探测步骤。"""
        for seg in suspect_segments:
            contribution = seg.get("contribution", 0)
            if contribution > 0.3 and "channel_quality_decline" in self.tree:
                prior = round(contribution * 0.4, 3)
                self.tree["channel_quality_decline"].score = round(
                    self.tree["channel_quality_decline"].score + prior, 3)
                self.tree["channel_quality_decline"].evidence_log.append({
                    "rule_id": "java_prior", "hypothesis": "channel_quality_decline",
                    "desc": f"Java检测层预定位：{seg.get('dimension')}={seg.get('value')} 贡献度{contribution:.0%}",
                    "detail": f"基线值{seg.get('baselineValue')} → 异常期{seg.get('anomalyValue')}，样本量{seg.get('sampleSize')}",
                    "weight": 0.4, "confidence": contribution,
                    "contribution": prior, "source_probe": "java_suspect_segments",
                    "judge_type": "external_prior",
                })
        self.trace.append({
            "step_type": "think", "round": 0,
            "text": f"Java预定位注入：{len(suspect_segments)} 个可疑细分，已更新对应假设先验分",
        })

    # ── Step 0b: 历史案例先验（巧思1：RAG × Agent 融合） ──
    def _seed_prior(self, phenomenon: str):
        """排查开始前先检索历史案例库：命中案例的 root_cause_tag
        给对应假设一个先验分（0.1 × 相似度），让搜索从有经验的方向出发。"""
        from tools.retrieval import retrieve_historical_case
        result = retrieve_historical_case(phenomenon, top_k=3)
        self.tool_calls += 1
        seeded = []
        for hit in result["hits"]:
            tag = hit.get("root_cause_tag")
            if tag in self.tree and hit["similarity"] > 0.05:
                prior = round(0.1 * hit["similarity"], 3)
                self.tree[tag].score = round(self.tree[tag].score + prior, 3)
                self.tree[tag].evidence_log.append({
                    "rule_id": "prior", "hypothesis": tag,
                    "desc": f"历史案例先验（{hit['case_id']}）",
                    "detail": f"相似案例: {hit['phenomenon'][:50]}… → {hit['conclusion'][:40]}",
                    "weight": 0.1, "confidence": hit["similarity"],
                    "contribution": prior, "source_probe": "prior_retrieval",
                    "judge_type": "rag_prior",
                })
                seeded.append((tag, prior, hit["case_id"]))
        self.trace.append({
            "step_type": "think", "round": 0,
            "text": f"Step 0 历史案例先验检索「{phenomenon}」: "
                    + (f"命中 {seeded}，对应假设获得先验分" if seeded else "无相似历史案例，从零开始排查"),
        })

    # ── 主循环 ───────────────────────────────────────
    def run(
        self,
        phenomenon: str = "新增用户FPD30逾期率近期环比明显上升",
        metric_name: str = "fpd30_rate",
        baseline_range: str = "2026-06-01~2026-07-19",
        anomaly_range: str = "2026-07-20~2026-07-30",
        direction: str = "UP",
        anomaly_type: str = "metric_deviation",
        suspect_segments: list = None,
    ):
        # 用动态参数重建假设树（替换掉 __init__ 里的静态默认树）
        from agent.hypotheses import build_hypothesis_tree
        self.tree = build_hypothesis_tree(
            metric_name=metric_name,
            baseline_range=baseline_range,
            anomaly_range=anomaly_range,
            phenomenon=phenomenon,
        )
        self.anomaly_type = anomaly_type
        self.direction = direction
        # 打分函数按此日期切基线期/异常期，不再写死具体日期
        self.split_date = anomaly_range.split("~")[0]
        self._seed_prior(phenomenon)
        # Java 传来的可疑细分直接注入先验分（跳过对应的探测步骤）
        if suspect_segments:
            self._inject_suspect_segments(suspect_segments)
        for rnd in range(1, config.MAX_ROUNDS + 1):
            hyp = self._pick_hypothesis()
            if hyp is None or self.tool_calls >= config.MAX_TOOL_CALLS:
                break

            self.trace.append({
                "step_type": "think", "round": rnd,
                "text": f"当前分数排序: {[(h.hyp_id, h.score) for h in self._ranked()]}；"
                        f"优先展开假设「{hyp.description}」（分数 {hyp.score}，"
                        f"剩余待验证子问题 {sum(p.status=='pending' for p in hyp.probes)} 个）",
            })

            pending = [p for p in hyp.probes if p.status == "pending"]
            for probe in pending[:config.MAX_EXPAND_PER_ROUND]:
                if self.tool_calls >= config.MAX_TOOL_CALLS:
                    break
                out = run_probe(probe)
                self.tool_calls += 1
                fired = apply_rules(probe.node_id, out["raw_result"], EVIDENCE_RULES, self.tree, self.split_date)
                self.trace.append({
                    "step_type": "probe", "round": rnd,
                    "probe_id": probe.node_id, "hypothesis": probe.hypothesis,
                    "question": probe.question, "tool_name": probe.tool_name,
                    "tool_args": probe.tool_args,
                    "raw_result": out["raw_result"], "digest": out["digest"],
                    "evidence_fired": fired, "folded": False,
                })

            self._fold_context()

            if self._check_converged():
                self.converged = True
                self.trace.append({
                    "step_type": "think", "round": rnd,
                    "text": f"最高分假设领先幅度超过 {config.CONVERGE_MARGIN}，进入自我核验",
                })
                break

        self._reflect()
        self._finalize()
        return self.final

    # ── Reflect 自我核验（巧思3） ─────────────────────
    def _reflect(self):
        """反问：如果当前第一名是错的，最可能被推翻的证据是什么？
        策略：给排名第二的假设一次补查机会（若它还有 pending probe），
        用对立证据检验第一名的稳固性。"""
        r = self._ranked()
        if len(r) < 2:
            return
        runner_up = r[1]
        pending = [p for p in runner_up.probes if p.status == "pending"]
        if not pending or self.tool_calls >= config.MAX_TOOL_CALLS:
            self.trace.append({
                "step_type": "reflect",
                "text": f"自我核验：次优假设「{runner_up.description}」已无未验证子问题或预算耗尽，"
                        f"结论无需修正",
            })
            return

        probe = pending[0]
        self.trace.append({
            "step_type": "reflect",
            "text": f"自我核验：若结论有误，最可能的替代解释是「{runner_up.description}」，"
                    f"补查其未验证子问题「{probe.question}」",
        })
        out = run_probe(probe)
        self.tool_calls += 1
        fired = apply_rules(probe.node_id, out["raw_result"], EVIDENCE_RULES, self.tree, self.split_date)
        self.trace.append({
            "step_type": "probe", "round": -1,
            "probe_id": probe.node_id, "hypothesis": probe.hypothesis,
            "question": probe.question, "tool_name": probe.tool_name,
            "tool_args": probe.tool_args,
            "raw_result": out["raw_result"], "digest": out["digest"],
            "evidence_fired": fired, "folded": False, "is_reflect": True,
        })

    # ── 输出 ─────────────────────────────────────────
    # 当前四个假设（渠道质量/审批口径/宏观季节性/数据质量）是针对
    # "指标数值类"异常（metric_deviation）设计的；model_degradation /
    # distribution_shift 传进来时排查仍会跑完，但结论只做参考标注，不假装适配。
    SPECIALIZED_ANOMALY_TYPES = {"metric_deviation"}

    def _finalize(self):
        r = self._ranked()
        top = r[0]
        low_confidence = top.score < config.MIN_CONFIDENCE_FLOOR
        specialized = self.anomaly_type in self.SPECIALIZED_ANOMALY_TYPES
        self.final = {
            "conclusion": ("证据不足以归入已知归因类型，建议人工复核" if low_confidence
                            else top.description),
            "conclusion_id": "other_unclassified" if low_confidence else top.hyp_id,
            "confidence": top.score,
            "low_confidence": low_confidence,
            "runner_up": {"desc": r[1].description, "score": r[1].score} if len(r) > 1 else None,
            "converged": self.converged,
            "tool_calls_used": self.tool_calls,
            "budget": config.MAX_TOOL_CALLS,
            "anomaly_type": self.anomaly_type,
            "direction": self.direction,
            "specialized": specialized,
            "note": None if specialized else (
                f"当前假设树针对指标数值类异常（metric_deviation）设计，"
                f"anomaly_type={self.anomaly_type} 尚无专属假设树，结论仅供参考"
            ),
            "ranking": [{"hyp_id": h.hyp_id, "desc": h.description, "score": h.score,
                         "evidence": h.evidence_log} for h in r],
        }


if __name__ == "__main__":
    import json
    orch = Orchestrator()
    final = orch.run()
    print(json.dumps(final, ensure_ascii=False, indent=2, default=str))
