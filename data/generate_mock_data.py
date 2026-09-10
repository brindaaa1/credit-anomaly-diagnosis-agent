"""生成带有"埋点异常"的模拟助贷数据。

埋的故事线（演示时 agent 要能顺藤摸瓜挖出来的真相）：
  2026-07-20 起，渠道 channel_C 放量（占比从 ~10% 涨到 ~35%），
  且该渠道用户 credit_score 系统性偏低 → 新增 cohort 的早期逾期率被拉高。
  即：真相 = "渠道质量下滑"，而不是审批口径变化（审批线全程未动）。

运行: python data/generate_mock_data.py
输出: data/mock_loans.csv （放款粒度，一行一笔借款）
"""
import numpy as np
import pandas as pd
import os

rng = np.random.default_rng(42)

START, END = "2026-06-01", "2026-07-30"
ANOMALY_START = pd.Timestamp("2026-07-20")

CHANNELS = ["channel_A", "channel_B", "channel_C"]
CITY_TIERS = ["T1", "T2", "T3"]
AGE_GROUPS = ["18-25", "26-35", "36-45", "46+"]


def sample_day(date: pd.Timestamp) -> pd.DataFrame:
    n = int(rng.normal(400, 30))  # 每日放款笔数

    is_anomaly = date >= ANOMALY_START
    # 渠道占比：异常期 channel_C 放量
    p = [0.55, 0.35, 0.10] if not is_anomaly else [0.42, 0.23, 0.35]
    channel = rng.choice(CHANNELS, size=n, p=p)

    # credit_score：channel_C 用户天然更低（均值低 50 分）
    base = rng.normal(660, 45, size=n)
    score = np.where(channel == "channel_C", base - 50, base).clip(300, 850)

    # 审批线全程不变（580 分），用于证伪"审批口径变化"假设
    approved = score >= 580

    # 早期逾期概率：与 score 负相关；channel_C 额外携带评分未覆盖的
    # "薄档案"风险（+9pp）—— 现实中新渠道用户风险常超出评分卡刻画范围
    p_overdue = np.clip(0.55 - score / 1600, 0.01, 0.5)
    p_overdue = np.where(channel == "channel_C", p_overdue + 0.09, p_overdue)
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
        "fpd30": is_overdue & approved,  # 首期30天逾期（只有放款成功才有逾期）
    })
    return df[df["approved"]]  # 只保留放款成功的


def main():
    days = pd.date_range(START, END, freq="D")
    df = pd.concat([sample_day(d) for d in days], ignore_index=True)
    out = os.path.join(os.path.dirname(__file__), "mock_loans.csv")
    df.to_csv(out, index=False)
    daily = df.groupby("date")["fpd30"].mean()
    print(f"写入 {out}: {len(df)} 行")
    print(f"异常前 FPD30 均值: {daily[daily.index < '2026-07-20'].mean():.3%}")
    print(f"异常后 FPD30 均值: {daily[daily.index >= '2026-07-20'].mean():.3%}")


if __name__ == "__main__":
    main()
