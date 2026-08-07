FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./

COPY src ./src

RUN uv sync \
    --frozen \
    --no-dev

COPY data/input ./data/input
COPY scripts ./scripts
COPY schemas ./schemas

RUN mkdir -p \
    /app/data/output \
    /app/cache/http \
    /app/cache/parsed \
    /app/reports

ENTRYPOINT ["fundscraper"]
CMD ["--help"]