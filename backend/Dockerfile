FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for building C extensions (xgboost, numpy, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the lockfile (exact pins exported
# from uv.lock) so a rebuild NEVER re-resolves to newer libraries.
# Model artifacts in the models volume persist across image rebuilds;
# a drifting sklearn/xgboost would unpickle them under a different
# version than they were trained with. Regenerate after editing
# pyproject deps:  uv lock && uv export --frozen --no-dev --no-hashes \
#   --no-emit-project -o requirements.lock.txt
COPY pyproject.toml requirements.lock.txt ./
RUN mkdir -p src/yolovest && \
    touch src/yolovest/__init__.py && \
    pip install --no-cache-dir pip --upgrade && \
    pip install --no-cache-dir -r requirements.lock.txt && \
    pip install --no-cache-dir --no-deps .

# Copy source and reinstall (now with actual code)
COPY src/ src/
COPY migrations/ migrations/
RUN pip install --no-cache-dir --no-deps .

# --- Production stage ---
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code, migrations, and operational scripts
# (offline experiment harness etc. — run via docker exec)
COPY --from=builder /app/src/ src/
COPY --from=builder /app/migrations/ migrations/
COPY scripts/ scripts/
COPY config.example.yaml config.example.yaml

# Create data directories
RUN mkdir -p /app/data /app/backups /app/models /app/logs

# Non-root user for security
RUN useradd -m -s /bin/bash yolovest && \
    chown -R yolovest:yolovest /app
USER yolovest

# Set PYTHONPATH so yolovest package is importable
ENV PYTHONPATH=/app/src
ENV TZ=Asia/Kolkata

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=180s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

ENTRYPOINT ["python", "-m", "yolovest.main"]
CMD ["--config", "config.yaml"]
