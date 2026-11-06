FROM python:3.12-slim

## 城市孪生 MCP 服务（SSE）——给 Nexent 接入用
## 只装运行 MCP 工具链需要的最小依赖：pydantic（契约校验）+ numpy（业务资产检索的向量层）。
## 业务代码全部来自仓库 src/，不在这里复制第二份。

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir "pydantic==2.14.1" "numpy>=2.5.4"

COPY src/ /app/src/
COPY samples/ /app/samples/

## 业务资产检索索引在构建期就建好：镜像自带，不依赖宿主机目录，
## 也让 doc_search 的返回与 samples/assets/urban 的资产版本严格对应。
## embedder 用 hash——无语义但零依赖；换真 embedding 时同时换索引与 embedder。
RUN python -m decision_poc.rag build --registered --embed hash --out /app/artifacts/rag_index_assets --quiet

EXPOSE 8000

## 健康检查：Nexent 注册前可先 curl /health 看 15 个工具是否在线
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)"

CMD ["python", "-m", "decision_poc.mcp_sse", "--host", "0.0.0.0", "--port", "8000"]
