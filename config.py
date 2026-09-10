"""全局配置。

MOCK_LLM=True 时不调用真实 API，用规则模拟 LLM 判断，
方便在没有 key 的环境下跑通全流程 demo。
"""
import os

# ── LLM ──────────────────────────────────────────────
# Kimi (Moonshot) 兼容 OpenAI SDK，换 base_url 即可
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.moonshot.cn/v1")
LLM_API_KEY = os.getenv("MOONSHOT_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "moonshot-v1-8k")
MOCK_LLM = os.getenv("MOCK_LLM", "1") == "1"   # 默认 mock，接好 key 后设为 0

# ── Agent 预算控制（"巧思4"：聪明地查，不是暴力地查） ──
MAX_TOOL_CALLS = 8        # 整个排查过程最多调用工具次数
MAX_EXPAND_PER_ROUND = 2  # 每轮最多展开的子节点数
CONVERGE_MARGIN = 0.30    # 最高分假设领先第二名多少即收敛
MAX_ROUNDS = 6            # 主循环最大轮数
MIN_CONFIDENCE_FLOOR = 0.30  # 最高分假设本身低于此值，不强行归类，返回 other_unclassified

# ── Context 折叠（cache breakpoint 思路） ─────────────
# 最近 K 次工具调用保留全文，更早的折叠成摘要
CONTEXT_WINDOW_K = 3

# ── 数据 ─────────────────────────────────────────────
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "mock_loans.csv")

# 公司数据库直连（生产环境用，留空则走上面的 mock CSV）。
# 只要设了 DB_HOST，tools/dashboard.py 就会走 _load_from_db()，其余 agent 逻辑不用动。
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "")
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
CASE_DB_PATH = os.path.join(os.path.dirname(__file__), "kb", "historical_cases.json")
KB_PATH = os.path.join(os.path.dirname(__file__), "kb", "business_kb.md")
CASE_CANDIDATE_PATH = os.path.join(os.path.dirname(__file__), "kb", "case_candidates.json")

# ── 外部工具：web_search（第一个访问外部网络的工具） ────
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
MOCK_WEB_SEARCH = os.getenv("MOCK_WEB_SEARCH", "1") == "1"
WEB_SEARCH_TIMEOUT = 10
MOCK_WEB_RESULTS_PATH = os.path.join(os.path.dirname(__file__), "kb", "mock_web_results.json")

# ── 案例入库 rubric（诊断结论要不要沉淀成新案例） ──────
AUTO_ADMIT_CASES = os.getenv("AUTO_ADMIT_CASES", "1") == "1"
MIN_CASE_CONFIDENCE = 0.5  # 低于此置信度不进入案例入库评估，即使已收敛
