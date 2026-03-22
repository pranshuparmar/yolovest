FROM python:3.12-slim AS base

WORKDIR /app

# System deps for aiosqlite, aiohttp, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir pip --upgrade && \
    pip install --no-cache-dir . 2>/dev/null || \
    pip install --no-cache-dir \
      pydantic pyyaml aiosqlite aiohttp pandas \
      fastapi uvicorn websockets httpx \
      python-telegram-bot

# Copy source code
COPY src/ src/
COPY migrations/ migrations/
COPY config.example.yaml config.example.yaml

# Install the package
RUN pip install --no-cache-dir -e . 2>/dev/null || true

# Create data directories
RUN mkdir -p /app/data /app/backups /app/models /app/logs

# Non-root user for security
RUN useradd -m -s /bin/bash yolovest && \
    chown -R yolovest:yolovest /app
USER yolovest

# Expose dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

# Default command: run with config
ENTRYPOINT ["python", "-m", "yolovest.main"]
CMD ["--config", "config.yaml"]
