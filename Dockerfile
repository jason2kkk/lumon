# ---- 阶段 1: 构建前端 ----
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 阶段 2: 运行后端 ----
FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY server.py ./
COPY backend/ ./backend/
COPY prompts/ ./prompts/
COPY data/demo/ ./data/demo/

COPY --from=frontend-build /app/frontend/dist ./frontend/dist

RUN mkdir -p data/cache data/reports data/poc_evaluations data/sessions

# 本地自托管容器不需要 root 权限运行应用。
RUN addgroup --system --gid 10001 lumon && \
    adduser --system --uid 10001 --ingroup lumon --no-create-home lumon && \
    chown -R lumon:lumon /app

USER lumon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
