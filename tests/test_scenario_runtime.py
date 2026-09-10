"""use_scenario_data()：临时替换 config.DATA_PATH 供 dashboard 读取，
退出 with 块时（含异常）必须恢复原路径、清空 dashboard 缓存。"""
import pandas as pd
import pytest

import config
import tools.dashboard as dashboard
from data.scenario_runtime import use_scenario_data


def _make_df(n_rows: int) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-07-01"] * n_rows,
        "user_id": [f"u{i}" for i in range(n_rows)],
        "channel": ["channel_A"] * n_rows,
        "city_tier": ["T1"] * n_rows,
        "age_group": ["26-35"] * n_rows,
        "credit_score": [650] * n_rows,
        "approved": [True] * n_rows,
        "loan_amount": [5000] * n_rows,
        "fpd30": [False] * n_rows,
    })


def test_use_scenario_data_points_dashboard_at_new_data(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with use_scenario_data(_make_df(7)):
        assert config.DATA_PATH != str(original_csv)
        result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
        assert result["records"][0]["value"] == 7

    assert config.DATA_PATH == str(original_csv)
    dashboard._load.cache_clear()
    result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
    assert result["records"][0]["value"] == 3


def test_use_scenario_data_restores_path_after_exception(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with pytest.raises(ValueError, match="boom"):
        with use_scenario_data(_make_df(7)):
            raise ValueError("boom")

    assert config.DATA_PATH == str(original_csv)


def test_use_scenario_data_none_disables_dashboard(tmp_path, monkeypatch):
    original_csv = tmp_path / "original.csv"
    _make_df(3).to_csv(original_csv, index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(original_csv))
    dashboard._load.cache_clear()

    with use_scenario_data(None):
        assert config.DATA_PATH is None

    assert config.DATA_PATH == str(original_csv)
    dashboard._load.cache_clear()
    result = dashboard.query_dashboard("loan_count", "2026-07-01~2026-07-01")
    assert result["records"][0]["value"] == 3
