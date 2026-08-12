# Agent Budget Controller — production image.
#
# Two stages so the runtime layer carries no build toolchain. The wheels are
# built once and copied in, which also keeps the final image small enough to
# pull comfortably onto a t3.micro.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Only the requirements first, so a code change does not invalidate the
# dependency layer.
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is used by the container healthcheck below.
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app mock_llm ./mock_llm
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app loadgen ./loadgen
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./alembic.ini

USER app
EXPOSE 8000

# /health reports Redis and PostgreSQL separately and returns 503 if either is
# down, so it is a real readiness signal rather than "the process is alive".
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
