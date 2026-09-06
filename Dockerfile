FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    DISPLAY=:99
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.16 \
    && uv sync --frozen --no-dev --no-install-project \
    && .venv/bin/python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 runner \
    && mkdir -p /data \
    && chown runner:runner /data

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"
COPY docker/entrypoint.py /app/entrypoint.py
USER runner
ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["--config", "/config/config.yaml", "--data-dir", "/data", "run", "--keep-alive"]
