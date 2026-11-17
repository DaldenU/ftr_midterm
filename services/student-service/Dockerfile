# Build from the repository root:  docker build -f services/student-service/Dockerfile -t uni-student .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 FTLIB_PATH=/app/shared
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY shared /app/shared
COPY services/student-service/app.py /app/app.py
RUN useradd -r -u 10001 app   # non-root; code stays root-owned and read-only for the app user
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning", "--no-access-log"]
