# Multi-stage production Dockerfile
# Stage 1: Build & Dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --user --no-warn-script-location -r requirements.txt


# Stage 2: Minimal Runtime Image
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/home/appuser/.local/bin:${PATH}" \
    HOST=0.0.0.0 \
    PORT=8000 \
    SQLITE_PATH=/app/data/board.db \
    LLM_BACKEND=auto

# Security: create non-root user and persistent data directory
RUN useradd -u 1000 -m -s /bin/bash appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

# Copy python dependencies from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application source
COPY --chown=appuser:appuser src/ /app/src/
COPY --chown=appuser:appuser requirements.txt /app/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, os; p = os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz').read()" || exit 1

ENTRYPOINT ["python", "src/main.py"]
CMD ["serve"]
