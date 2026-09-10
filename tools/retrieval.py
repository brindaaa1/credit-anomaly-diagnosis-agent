"""工具2 & 3: 历史案例检索 + 业务知识库检索（只读 RAG）。

用本地 BGE embedding（BAAI/bge-small-zh-v1.5，sentence-transformers）+ 余弦相似度检索，
中文语义匹配比 TF-IDF 更准，且不需要 API key、可离线跑（首次运行会从 HuggingFace
下载一次模型权重，之后走本地缓存）。
"""
from functools import lru_cache
import json
import re
from sentence_transformers import SentenceTransformer, util
import config

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(_MODEL_NAME)


def _rank(query: str, docs: list[dict], text_key: str, top_k: int) -> list[dict]:
    if not docs:
        return []
    model = _model()
    doc_embeddings = model.encode([d[text_key] for d in docs], normalize_embeddings=True)
    query_embedding = model.encode(_QUERY_INSTRUCTION + query, normalize_embeddings=True)
    sims = util.cos_sim(query_embedding, doc_embeddings).flatten().tolist()
    ranked = sorted(zip(sims, docs), key=lambda x: -x[0])[:top_k]
    return [{**d, "similarity": round(float(s), 3)} for s, d in ranked if s > 0]


def retrieve_historical_case(query_text: str, top_k: int = 3) -> dict:
    with open(config.CASE_DB_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    hits = _rank(query_text, cases, "phenomenon", top_k)
    return {"tool_name": "retrieve_historical_case", "query": query_text, "hits": hits}


def _load_kb_md(path: str) -> list[dict]:
    """解析 `## term` + 正文 的简单 Markdown KB，比 JSON 更易手动增补条目。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    entries = []
    for block in re.split(r"\n(?=## )", text.strip()):
        term, _, content = block.partition("\n")
        term = term.removeprefix("## ").strip()
        content = content.strip()
        if term and content:
            entries.append({"term": term, "content": content})
    return entries


def retrieve_business_kb(query_text: str, top_k: int = 3) -> dict:
    entries = _load_kb_md(config.KB_PATH)
    hits = _rank(query_text, entries, "content", top_k)
    return {"tool_name": "retrieve_business_kb", "query": query_text, "hits": hits}
