"""场景数据注入：让 tools/dashboard.py 在一个 with 块内读到指定的 DataFrame，
而不是默认的 config.DATA_PATH。

tools/dashboard.py::_load() 用 @lru_cache(maxsize=1) 缓存全局唯一一份数据，
只换 config.DATA_PATH 不清缓存的话，第二次调用还是读旧数据；用 context manager
包一层，保证正常退出和异常退出时都会恢复原路径、清空缓存，不污染同一进程内
后续对默认数据的读取。

df 为 None 时不写任何文件，直接把 config.DATA_PATH 设为 None——
query_dashboard() 遇到 None 会直接返回"数据源未接入"，不会尝试读取任何 CSV
（包括默认的 data/mock_loans.csv），用于「自定义异常输入」这种没有真实明细
数据的场景，避免用本地 mock 数据冒充成看板查询结果。
"""
from contextlib import contextmanager
import os
import shutil
import tempfile

import pandas as pd

import config
import tools.dashboard as dashboard


@contextmanager
def use_scenario_data(df: pd.DataFrame | None):
    original_path = config.DATA_PATH
    tmp_dir = None
    try:
        if df is None:
            config.DATA_PATH = None
        else:
            tmp_dir = tempfile.mkdtemp(prefix="scenario_data_")
            tmp_csv = os.path.join(tmp_dir, "data.csv")
            df.to_csv(tmp_csv, index=False)
            config.DATA_PATH = tmp_csv
        dashboard._load.cache_clear()
        yield
    finally:
        config.DATA_PATH = original_path
        dashboard._load.cache_clear()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
