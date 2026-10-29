FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AFL_DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml requirements-lock.txt README.md LICENSE ./
COPY audio_learning ./audio_learning
RUN python -m pip install --no-cache-dir -c requirements-lock.txt . \
    && groupadd --gid 10001 zouma \
    && useradd --uid 10001 --gid 10001 --no-create-home zouma \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "audio_learning.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
