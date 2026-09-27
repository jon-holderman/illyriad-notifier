FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy NOTIFIER_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home notifier && mkdir /data && chown notifier:notifier /data
ENV PATH="/app/.venv/bin:$PATH"
USER notifier
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["illyriad-notifier", "serve", "--host", "0.0.0.0", "--port", "8080"]
