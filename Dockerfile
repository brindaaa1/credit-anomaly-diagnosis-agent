FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# 先装 CPU 版 torch：不这样的话 pip 会把 sentence-transformers 依赖的 torch 解析成
# 默认（带完整 CUDA 工具链）版本，容器里根本用不上 GPU，白白多下几个 G。
RUN pip install --no-cache-dir --timeout 120 --retries 5 torch --index-url https://download.pytorch.org/whl/cpu
# --timeout/--retries: 本机构建时观察到大包下载中途超时，放宽容忍度避免偶发网络波动打断构建
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY . .
RUN python data/generate_mock_data.py

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
