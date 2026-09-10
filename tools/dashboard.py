"""工具1: query_dashboard —— 看板数据接口（只读）。

真实环境里这一层换成公司看板 API 的封装，schema 不变，上层 agent 逻辑不用动。
demo 环境里直接读 mock_loans.csv 用 pandas 计算。

支持的 metric:
  fpd30_rate       首期30天逾期率
  loan_count       放款笔数
  avg_credit_score 放款用户平均信用分
  channel_share    渠道占比（必须配合 dimensions=["channel"]）
"""
import pandas as pd
from functools import lru_cache
import config


@lru_cache(maxsize=1)
def _load() -> pd.DataFrame:
    df = pd.read_csv(config.DATA_PATH)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _load_from_db() -> pd.DataFrame:
    """── 生产环境接入点 ──

    唯一需要真实实现的地方。用 `config.DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/
    `DB_PASSWORD` 连公司数据库（具体用哪个 driver 视数据库类型而定，如
    pyodbc/psycopg2/pymysql），查出跟 `data/mock_loans.csv` 一样的列，
    返回同样 schema 的 DataFrame，其余代码（下面的 groupby 聚合、以及更上层的
    agent 逻辑）不用改：

      date (datetime64), user_id, channel, credit_score, fpd30 (bool)

    写好以后可以按需加缓存/日期范围下推，这里先给最简单的全量查询占位。
    """
    raise NotImplementedError(
        "还没接公司数据库：填这个函数，连 config.DB_HOST 指向的库，"
        "查出 date/user_id/channel/credit_score/fpd30 这几列即可"
    )


def query_dashboard(metric_name: str, date_range: str, dimensions: list | None = None) -> dict:
    """date_range 格式: 'YYYY-MM-DD~YYYY-MM-DD'。dimensions 为空则返回时间序列。"""
    if config.DB_HOST:
        df = _load_from_db()
    elif config.DATA_PATH:
        df = _load()
    else:
        return {"tool_name": "query_dashboard",
                "error": "数据源未接入：生产环境将对接公司数据库，当前演示模式下不可用"}

    start, end = date_range.split("~")
    mask = (df["date"] >= start) & (df["date"] <= end)
    sub = df[mask]
    if sub.empty:
        return {"tool_name": "query_dashboard", "error": f"no data in {date_range}"}

    group_keys = ["date"] + (dimensions or [])

    if metric_name == "fpd30_rate":
        out = sub.groupby(group_keys)["fpd30"].mean().rename("value")
    elif metric_name == "loan_count":
        out = sub.groupby(group_keys)["user_id"].count().rename("value")
    elif metric_name == "avg_credit_score":
        out = sub.groupby(group_keys)["credit_score"].mean().rename("value")
    elif metric_name == "channel_share":
        cnt = sub.groupby(["date", "channel"])["user_id"].count()
        out = (cnt / cnt.groupby("date").transform("sum")).rename("value")
    else:
        return {"tool_name": "query_dashboard", "error": f"unknown metric {metric_name}"}

    result = out.reset_index()
    result["date"] = result["date"].dt.strftime("%Y-%m-%d")

    # 返回紧凑摘要 + 明细，供 agent 和规则引擎两头用
    return {
        "tool_name": "query_dashboard",
        "metric": metric_name,
        "date_range": date_range,
        "dimensions": dimensions or [],
        "summary": {
            "mean": round(float(result["value"].mean()), 4),
            "min": round(float(result["value"].min()), 4),
            "max": round(float(result["value"].max()), 4),
        },
        "records": result.to_dict(orient="records"),
    }
