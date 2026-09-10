"""data/scenarios.py 的量级校验：确认每类场景生成的数据真的会触发/不触发
对应的 agent/scoring.py 数值规则，而不是"看起来差不多但量级不够"。

用真实的 tools/dashboard.query_dashboard + agent/scoring 里的规则函数
端到端验证，不重新实现一遍判断逻辑。
"""
import pandas as pd

import config
import tools.dashboard as dashboard
from agent.scoring import (
    _channel_share_shift, _channel_overdue_gap, _score_drop, _count_jump,
)
from data.scenarios import (
    SCENARIOS, iter_runs,
    _gen_channel_quality_decline, _gen_approval_policy_change,
    _gen_macro_seasonality, _gen_data_quality_issue, _gen_ambiguous_negative,
    _gen_policy_and_channel_mixed,
)

SPLIT = "2026-07-20"
FULL_RANGE = "2026-06-01~2026-07-30"
ANOMALY_RANGE = "2026-07-20~2026-07-30"


def _query(df: pd.DataFrame, tmp_path, monkeypatch, metric_name, date_range, dimensions=None):
    csv_path = tmp_path / "scenario.csv"
    df.to_csv(csv_path, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(csv_path))
    dashboard._load.cache_clear()
    try:
        return dashboard.query_dashboard(metric_name, date_range, dimensions)
    finally:
        dashboard._load.cache_clear()


def test_channel_quality_decline_triggers_share_and_overdue_rules(tmp_path, monkeypatch):
    df = _gen_channel_quality_decline(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert triggered

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    triggered, _ = _channel_overdue_gap(overdue, SPLIT)
    assert triggered


def test_approval_policy_change_triggers_score_drop_but_not_channel_rules(tmp_path, monkeypatch):
    df = _gen_approval_policy_change(seed=42)

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    triggered, _ = _score_drop(score, SPLIT)
    assert triggered

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert not triggered


def test_macro_seasonality_raises_overdue_without_channel_or_score_signal(tmp_path, monkeypatch):
    df = _gen_macro_seasonality(seed=42)

    baseline_rate = df[df["date"] < SPLIT]["fpd30"].mean()
    anomaly_rate = df[df["date"] >= SPLIT]["fpd30"].mean()
    assert anomaly_rate - baseline_rate > 0.03  # 确实存在整体抬升信号，不是纯噪声

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    assert not _channel_share_shift(share, SPLIT)[0]

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    assert not _score_drop(score, SPLIT)[0]


def test_data_quality_issue_triggers_count_jump(tmp_path, monkeypatch):
    df = _gen_data_quality_issue(seed=42)
    count = _query(df, tmp_path, monkeypatch, "loan_count", ANOMALY_RANGE)
    triggered, _ = _count_jump(count, SPLIT)
    assert triggered


def test_ambiguous_negative_triggers_no_numeric_rule(tmp_path, monkeypatch):
    df = _gen_ambiguous_negative(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    assert not _channel_share_shift(share, SPLIT)[0]

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    assert not _channel_overdue_gap(overdue, SPLIT)[0]

    score = _query(df, tmp_path, monkeypatch, "avg_credit_score", FULL_RANGE)
    assert not _score_drop(score, SPLIT)[0]

    count = _query(df, tmp_path, monkeypatch, "loan_count", ANOMALY_RANGE)
    assert not _count_jump(count, SPLIT)[0]


def test_policy_and_channel_mixed_triggers_share_shift_but_not_overdue_gap(tmp_path, monkeypatch):
    """信号刻意调弱：r1（占比变化>15pp）应该触发，r2（渠道逾期率高出均值50%）
    大概率不应该触发——跟 test_channel_quality_decline_triggers_share_and_overdue_rules
    对比着看，同一个假设方向，但证据强度不同。"""
    df = _gen_policy_and_channel_mixed(seed=42)

    share = _query(df, tmp_path, monkeypatch, "channel_share", FULL_RANGE, ["channel"])
    triggered, _ = _channel_share_shift(share, SPLIT)
    assert triggered

    overdue = _query(df, tmp_path, monkeypatch, "fpd30_rate", ANOMALY_RANGE, ["channel"])
    triggered, _ = _channel_overdue_gap(overdue, SPLIT)
    assert not triggered


def test_scenarios_registry_covers_all_hypotheses_and_seeds():
    assert set(SCENARIOS) == {
        "channel_quality_decline", "approval_policy_change",
        "macro_seasonality", "data_quality_issue", "ambiguous_negative",
        "policy_and_channel_mixed",
    }
    for scenario_type, spec in SCENARIOS.items():
        assert spec.scenario_type == scenario_type

    run_ids = [run_id for run_id, _df, _spec in iter_runs()]
    assert len(run_ids) == len(set(run_ids)) == 16
    assert "channel_quality_decline_s42" in run_ids
    assert SCENARIOS["ambiguous_negative"].ground_truth is None
    assert SCENARIOS["policy_and_channel_mixed"].ground_truth == "channel_quality_decline"
