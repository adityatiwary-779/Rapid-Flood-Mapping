# Build from the repo root:  docker build -f worker/Dockerfile -t flood-worker .
FROM python:3.11-slim
WORKDIR /app
COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt
COPY common common
COPY pipeline pipeline
COPY worker worker
ENV PORT=8080 PYTHONUNBUFFERED=1
CMD exec uvicorn worker.main:app --host 0.0.0.0 --port ${PORT}
