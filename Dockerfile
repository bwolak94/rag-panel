FROM python:3.12-slim

# Security: run as non-root
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --shell /bin/bash --create-home app

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies and project (cached layer — run as root so venv is writable at build time)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/

# Fix ownership so the app user can read the venv
RUN chown -R app:app /app

# Switch to non-root user
USER app

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Use venv's uvicorn directly (avoids uv trying to re-sync at startup)
CMD ["/app/.venv/bin/uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
