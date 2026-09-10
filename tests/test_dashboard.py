"""query_dashboard() 在 config.DATA_PATH 未配置（None）时的降级行为——
供「自定义异常输入」模式使用，避免在没有真实明细数据时误读任何本地 CSV。"""
import config
import tools.dashboard as dashboard


def test_query_dashboard_returns_error_when_data_path_is_none(monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", None)
    dashboard._load.cache_clear()

    result = dashboard.query_dashboard("fpd30_rate", "2026-06-01~2026-07-30")

    assert result["tool_name"] == "query_dashboard"
    assert "error" in result
    assert "records" not in result
    assert "summary" not in result
