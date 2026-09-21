FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY policy ./policy
COPY service ./service
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "service.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
