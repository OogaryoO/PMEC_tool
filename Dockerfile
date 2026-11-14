# 網頁服務（Streamlit）與背景工作（python -m src.worker ...）共用同一個映像。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# 建置時就下載 embedding 模型，避免 Cloud Run 冷啟動時才下載約 220MB
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBEDDING_MODEL'])"

COPY . .

# Cloud Run 以 PORT 指定監聽埠；secrets.toml 由 Cloud Run 從 Secret Manager 掛載到 /app/.streamlit/
CMD ["sh", "-c", "exec streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false"]
