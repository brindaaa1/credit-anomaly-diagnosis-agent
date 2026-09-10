"""tools/retrieval.py 的边界情况测试：文档列表为空时不应该抛异常。"""
from tools.retrieval import _rank


def test_rank_returns_empty_list_when_docs_empty():
    assert _rank("随便查询", [], "phenomenon", top_k=3) == []


def test_rank_still_works_with_normal_docs():
    docs = [
        {"phenomenon": "渠道放量导致逾期上升", "extra": 1},
        {"phenomenon": "宏观季节性波动", "extra": 2},
    ]
    hits = _rank("渠道放量 逾期", docs, "phenomenon", top_k=3)
    assert len(hits) >= 1
    assert hits[0]["phenomenon"] == "渠道放量导致逾期上升"


def test_business_kb_includes_new_risk_terms():
    from tools.retrieval import _load_kb_md
    import config

    entries = _load_kb_md(config.KB_PATH)
    terms = {e["term"] for e in entries}
    for expected in ["1m30 / 1m30_amount", "多头", "找黑能力", "睡眠用户", "戳额", "标的 / 发标", "新客额度 / 老客额度", "期数金额加权"]:
        assert expected in terms, f"缺少词条: {expected}"
