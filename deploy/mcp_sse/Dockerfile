FROM python:3.12-slim

## 城市孪生 MCP 服务（SSE）——给 Nexent 接入用
## 只装运行 MCP 工具链需要的最小依赖：pydantic（契约校验）。
## 业务代码全部来自仓库 src/，不在这里复制第二份。

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir "pydantic==2.14.1"

COPY src/ /app/src/
COPY samples/ /app/samples/

EXPOSE 8000

## 健康检查：Nexent 注册前可先 curl /health 看 14 个工具是否在线
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)"

CMD ["python", "-m", "decision_poc.mcp_sse", "--host", "0.0.0.0", "--port", "8000"]
