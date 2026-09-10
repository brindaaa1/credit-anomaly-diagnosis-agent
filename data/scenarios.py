"""场景化 mock 数据生成器：credit-anomaly-agent 的评估用多场景数据源。

data/generate_mock_data.py 只产出一份写死的"渠道质量下滑"故事线。
本模块把它推广成参数化的多场景生成器，服务于 scripts/eval_scenarios.py：
每类假设各配一个真值场景（+ 2 个不同随机种子的变体），外加一个无结构性
信号的模糊负例，用于检验 agent 在证据不足时是否仍给出高置信度结论。
其中 channel_quality_decline 额外配了一个信号调弱版
（policy_and_channel_mixed）：同样的真值方向，但渠道结构变化和渠道级
风险加成都刻意调弱到接近规则触发边界，用来跟清晰单因场景对比测试。

生成的 DataFrame schema 与 data/generate_mock_data.py 输出的 mock_loans.csv
完全一致（date/user_id/channel/city_tier/age_group/credit_score/approved/
loan_amount/fpd30），可以直接喂给 tools/dashboard.py。

不修改 data/generate_mock_data.py 本身——那是 README 快速开始命令依赖的
默认演示数据，保持独立。
"""
import numpy as np
import pandas as pd

from dataclasses import dataclass
from typing import Callable, Iterator

START, END = "2026-06-01", "2026-07-30"
ANOMALY_START = pd.Timestamp("2026-07-20")
CHANNELS = ["channel_A", "channel_B", "channel_C"]
CITY_TIERS = ["T1", "T2", "T3"]
AGE_GROUPS = ["18-25", "26-35", "36-45", "46+"]


# ── 公共构件 ─────────────────────────────────────────
def _draw_channel(n: int, rng: np.random.Generator, p=(0.55, 0.35, 0.10)) -> np.ndarray:
    return rng.choice(CHANNELS, size=n, p=list(p))


def _draw_score(n: int, rng: np.random.Generator, mu: float = 660, sigma: float = 45) -> np.ndarray:
    return rng.normal(mu, sigma, size=n).clip(300, 850)


def _base_p_overdue(score: np.ndarray) -> np.ndarray:
    return np.clip(0.55 - score / 1600, 0.01, 0.5)


def _assemble_day(date: pd.Timestamp, rng: np.random.Generator, channel: np.ndarray,
                   score: np.ndarray, cutoff: float, p_overdue: np.ndarray) -> pd.DataFrame:
    """给定一天的渠道分配/信用分/审批线/逾期概率，装配成放款记录，只保留放款成功的。"""
    n = len(channel)
    approved = score >= cutoff
    is_overdue = rng.random(n) < p_overdue
    df = pd.DataFrame({
        "date": date.strftime("%Y-%m-%d"),
        "user_id": [f"u_{date.strftime('%m%d')}_{i:04d}" for i in range(n)],
        "channel": channel,
        "city_tier": rng.choice(CITY_TIERS, size=n, p=[0.3, 0.4, 0.3]),
        "age_group": rng.choice(AGE_GROUPS, size=n, p=[0.25, 0.4, 0.25, 0.1]),
        "credit_score": score.round(0),
        "approved": approved,
        "loan_amount": rng.choice([2000, 5000, 8000, 12000, 20000], size=n),
        "fpd30": is_overdue & approved,
    })
    return df[df["approved"]]


def _days() -> list[pd.Timestamp]:
    return list(pd.date_range(START, END, freq="D"))


# ── 场景生成器 ────────────────────────────────────────
def _gen_channel_quality_decline(seed: int) -> pd.DataFrame:
    """真值：channel_quality_decline。channel_C 异常期放量（10%→35%），
    该渠道用户信用分系统性偏低（-50分），额外携带+9pp逾期风险。
    跟 data/generate_mock_data.py 的默认故事线同一套参数，只是可换随机种子。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        p = (0.42, 0.23, 0.35) if is_anomaly else (0.55, 0.35, 0.10)
        channel = _draw_channel(n, rng, p)
        base = _draw_score(n, rng)
        score = np.where(channel == "channel_C", (base - 50).clip(300, 850), base)
        p_overdue = _base_p_overdue(score)
        p_overdue = np.where(channel == "channel_C", np.clip(p_overdue + 0.09, 0.01, 0.9), p_overdue)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


def _gen_policy_and_channel_mixed(seed: int) -> pd.DataFrame:
    """真值（主要）：channel_quality_decline，但信号调弱到接近规则触发边界；
    同时该场景配合专项测试（tests/test_multi_factor_scenario.py）时会叠加一份
    "监管全面收紧"的 mock 检索内容（regulatory_policy_change 的证据来源不在这份
    DataFrame 里，是 web_search 的 mock 内容），模拟"渠道结构变化 + 监管政策收紧"
    同期发生、互相干扰排查的真实场景。
    channel_C 占比涨幅（10%→30%，nominal +20pp；经过580分审批线筛选后，
    query_dashboard 实测的真实占比涨幅在三个注册种子上分别约为
    +19.2pp/+20.9pp/+20.2pp（seed 42/101/202），相对 r1 的 15pp 阈值留了
    至少约4pp的实测安全边际，不是刚好卡线）、信用分降幅
    （-20分，明显弱于单因场景的-50分）、逾期风险加成（+4pp，弱于单因场景的+9pp，
    实测 r2 的"高出均值50%"缺口在三个种子上约为22%~32%，明显低于50%阈值，
    不会触发）都刻意调弱，让 channel_quality_decline
    只能拿到 r1 的部分证据，不是 r1+r2 同时确凿命中。
    跑常规批量评估（scripts/eval_scenarios.py）时只用得到这份数据本身，
    web_search 走默认 mock 内容（不含"全面收紧"），所以在那条路径下这个场景
    体现的是"channel_quality_decline 信号变弱、置信度降低"，不是完整的双假设模糊——
    完整的模糊场景需要专项测试额外 monkeypatch MOCK_WEB_RESULTS_PATH 才能看到，
    详见 tests/test_multi_factor_scenario.py 顶部说明。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        p = (0.50, 0.20, 0.30) if is_anomaly else (0.55, 0.35, 0.10)
        channel = _draw_channel(n, rng, p)
        base = _draw_score(n, rng)
        score = np.where(channel == "channel_C", (base - 20).clip(300, 850), base)
        p_overdue = _base_p_overdue(score)
        p_overdue = np.where(channel == "channel_C", np.clip(p_overdue + 0.04, 0.01, 0.9), p_overdue)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


def _gen_approval_policy_change(seed: int) -> pd.DataFrame:
    """真值：approval_policy_change。渠道结构、渠道级逾期风险均不变；
    异常期整体准入信用分分布下移30分（模拟审批口径事实上放宽），
    审批线（cutoff）数值本身保持580不变——纯 cutoff 下调在当前分数分布下
    不足以让均分降超过10分（截断正态分布的算术上限约3-4分），
    所以用分布整体下移来制造可测的信号，这是本次 mock 的简化处理。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        channel = _draw_channel(n, rng)
        mu = 630 if is_anomaly else 660
        score = _draw_score(n, rng, mu=mu)
        p_overdue = _base_p_overdue(score)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


def _gen_macro_seasonality(seed: int) -> pd.DataFrame:
    """真值：macro_seasonality。渠道结构、信用分分布全程不变；
    异常期所有渠道逾期概率同步+5pp（无渠道级差异，用来跟
    channel_quality_decline 的渠道特异性风险区分开）。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        is_anomaly = date >= ANOMALY_START
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        if is_anomaly:
            p_overdue = np.clip(p_overdue + 0.05, 0.01, 0.9)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


def _gen_data_quality_issue(seed: int) -> pd.DataFrame:
    """真值：data_quality_issue。真实逾期率/渠道结构/信用分全程平稳；
    异常期正中间那天模拟ETL任务失败，60%的放款记录丢失（分母塌陷），
    制造单日放款笔数跳变，同时不影响其他天的真实指标。"""
    rng = np.random.default_rng(seed)
    days = _days()
    anomaly_days = [d for d in days if d >= ANOMALY_START]
    glitch_day = anomaly_days[len(anomaly_days) // 2]
    frames = []
    for date in days:
        n = int(rng.normal(400, 30))
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        day_df = _assemble_day(date, rng, channel, score, 580, p_overdue)
        if date == glitch_day:
            keep_mask = rng.random(len(day_df)) < 0.4
            day_df = day_df[keep_mask]
        frames.append(day_df)
    return pd.concat(frames, ignore_index=True)


def _gen_ambiguous_negative(seed: int) -> pd.DataFrame:
    """无真值：全程使用同一套稳态参数，异常期和基线期只有随机噪声差异，
    没有任何结构性变化——用于检验 agent 在证据不足时是否仍给出高置信度结论。"""
    rng = np.random.default_rng(seed)
    frames = []
    for date in _days():
        n = int(rng.normal(400, 30))
        channel = _draw_channel(n, rng)
        score = _draw_score(n, rng)
        p_overdue = _base_p_overdue(score)
        frames.append(_assemble_day(date, rng, channel, score, 580, p_overdue))
    return pd.concat(frames, ignore_index=True)


# ── 场景注册表 ────────────────────────────────────────
@dataclass(frozen=True)
class ScenarioSpec:
    scenario_type: str
    ground_truth: str | None
    phenomenon: str
    generator: Callable[[int], pd.DataFrame]
    seeds: tuple[int, ...]


SCENARIOS: dict[str, ScenarioSpec] = {
    "channel_quality_decline": ScenarioSpec(
        "channel_quality_decline", "channel_quality_decline",
        "新增用户FPD30逾期率近期环比明显上升",
        _gen_channel_quality_decline, (42, 101, 202)),
    "policy_and_channel_mixed": ScenarioSpec(
        "policy_and_channel_mixed", "channel_quality_decline",
        "新增用户1m30逾期率近期上升，渠道结构和监管环境同期都有变化，信号强度弱于典型场景",
        _gen_policy_and_channel_mixed, (42, 101, 202)),
    "approval_policy_change": ScenarioSpec(
        "approval_policy_change", "approval_policy_change",
        "新增用户FPD30逾期率近期缓慢爬升，信用分分布疑似左移",
        _gen_approval_policy_change, (42, 101, 202)),
    "macro_seasonality": ScenarioSpec(
        "macro_seasonality", "macro_seasonality",
        "全渠道FPD30逾期率近期同步小幅上升，无单一渠道异常",
        _gen_macro_seasonality, (42, 101, 202)),
    "data_quality_issue": ScenarioSpec(
        "data_quality_issue", "data_quality_issue",
        "FPD30逾期率异常期内某日出现数值跳变",
        _gen_data_quality_issue, (42, 101, 202)),
    "ambiguous_negative": ScenarioSpec(
        "ambiguous_negative", None,
        "新增用户FPD30逾期率近期波动，暂无明确异常信号",
        _gen_ambiguous_negative, (42,)),
}


def iter_runs() -> Iterator[tuple[str, pd.DataFrame, ScenarioSpec]]:
    """产出 (run_id, dataframe, spec)，run_id 形如 channel_quality_decline_s42。"""
    for spec in SCENARIOS.values():
        for seed in spec.seeds:
            run_id = f"{spec.scenario_type}_s{seed}"
            yield run_id, spec.generator(seed), spec
