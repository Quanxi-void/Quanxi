FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-prod.txt ./requirements-prod.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-prod.txt

COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads/avatars \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Start with one worker on a small VPS; tune after measuring memory.
ENV HOST=0.0.0.0 PORT=8000 WEB_CONCURRENCY=1
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${HOST} --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*'"]
