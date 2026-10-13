FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system sage && useradd --system --gid sage --home-dir /app sage

COPY pyproject.toml README.md ./
COPY src ./src
COPY skills ./skills
COPY solutions ./solutions
COPY docs ./docs

RUN python -m pip install --upgrade pip && python -m pip install .

RUN mkdir -p /var/lib/sage && chown -R sage:sage /app /var/lib/sage
USER sage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

CMD ["uvicorn", "sage.app:app", "--host", "0.0.0.0", "--port", "8000"]
