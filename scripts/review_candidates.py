"""candidate 档案例的人工复核 CLI。

rubric（agent/curation.py）打出 candidate 档（admission_score 0.45~0.7）的诊断结论
不会自动写进 kb/historical_cases.json，先落在 kb/case_candidates.json 里等人过一遍眼。
这里就是那个"人"用的工具：逐条看现象/排查过程/结论，批准的话搬进正式案例库，
拒绝的话留在候选文件里打上 rejected 标记——不删除，candidates 文件本身就是审计记录。

用法：
    python scripts/review_candidates.py            # 交互式逐条复核
    python scripts/review_candidates.py --list      # 只列出待审条目，不进入交互
"""
import argparse
import json
import os

import config


def load_candidates() -> list[dict]:
    if not os.path.exists(config.CASE_CANDIDATE_PATH):
        return []
    with open(config.CASE_CANDIDATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_candidates(candidates: list[dict]) -> None:
    with open(config.CASE_CANDIDATE_PATH, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)


def apply_decision(candidates: list[dict], case_id: str, decision: str) -> dict:
    """decision: "approve" | "reject"。

    只处理 status=="pending" 的条目：已经审过的（approved/rejected）视为"没找到"，
    防止脚本中途重跑时被二次改写。原地修改 candidates 里对应条目的 status，不删除条目——
    candidates 文件本身就是审计记录，谁在什么时候批准/拒绝了什么要留痕可查。
    approve 时返回的 promoted_case 去掉了 status 字段，因为正式案例库（historical_cases.json）
    里的案例不带这个 candidate 专属状态字段。
    """
    for c in candidates:
        if c["case_id"] == case_id and c.get("status") == "pending":
            if decision == "approve":
                promoted = {k: v for k, v in c.items() if k != "status"}
                c["status"] = "approved"
                return {"found": True, "promoted_case": promoted}
            if decision == "reject":
                c["status"] = "rejected"
                return {"found": True, "promoted_case": None}
            raise ValueError(f"unknown decision: {decision!r}")
    return {"found": False, "promoted_case": None}


def promote_to_library(case: dict) -> None:
    """单人交互式脚本，不像 api.py 那样有并发写入的风险，不需要加锁。"""
    cases = []
    if os.path.exists(config.CASE_DB_PATH):
        with open(config.CASE_DB_PATH, encoding="utf-8") as f:
            cases = json.load(f)
    cases.append(case)
    with open(config.CASE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)


def _prompt_decision(candidate: dict) -> str:
    print(f"\n--- {candidate['case_id']}（admission_score={candidate.get('admission_score')}）---")
    print(f"现象: {candidate['phenomenon']}")
    print(f"排查: {candidate.get('investigation', '')}")
    print(f"结论: {candidate['conclusion']}（{candidate['root_cause_tag']}）")
    while True:
        choice = input("批准入库(y) / 拒绝(n) / 跳过(s): ").strip().lower()
        if choice in ("y", "n", "s"):
            return {"y": "approve", "n": "reject", "s": "skip"}[choice]
        print("请输入 y / n / s")


def main() -> None:
    parser = argparse.ArgumentParser(description="candidate 档案例人工复核")
    parser.add_argument("--list", action="store_true", help="只列出 pending 候选，不进入交互")
    args = parser.parse_args()

    candidates = load_candidates()
    pending = [c for c in candidates if c.get("status") == "pending"]
    if not pending:
        print("没有待审核的候选案例。")
        return

    if args.list:
        for c in pending:
            print(f"{c['case_id']}\t{c.get('admission_score')}\t{c['phenomenon'][:40]}")
        return

    for c in pending:
        decision = _prompt_decision(c)
        if decision == "skip":
            continue
        result = apply_decision(candidates, c["case_id"], decision)
        if decision == "approve" and result["promoted_case"]:
            promote_to_library(result["promoted_case"])
            print(f"已批准，写入 {config.CASE_DB_PATH}")
        else:
            print("已标记拒绝，保留在候选文件中作为记录")
        save_candidates(candidates)  # 每处理一条就落盘一次，避免中途退出丢进度


if __name__ == "__main__":
    main()
