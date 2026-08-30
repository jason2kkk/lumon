"""
server.py — FastAPI 入口

职责：CORS、静态文件 serve、路由挂载
启动：uvicorn server:app --reload --port 8000
"""

from pathlib import Path
import hmac
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api_routes import router

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(host: str | None) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def _origin_host(origin: str) -> str:
    try:
        parsed = urlsplit(origin.strip())
        if parsed.scheme not in {"http", "https"}:
            return ""
        return parsed.hostname or ""
    except ValueError:
        return ""


_local_only = _env_bool("LUMON_LOCAL_ONLY", True)
_trust_local_proxy = _env_bool("LUMON_TRUST_LOCAL_PROXY", False)
_access_token = os.getenv("LUMON_ACCESS_TOKEN", "").strip()
_allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "LUMON_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
try:
    _max_request_bytes = max(64 * 1024, int(os.getenv("LUMON_MAX_REQUEST_BYTES", str(8 * 1024 * 1024))))
except ValueError:
    _max_request_bytes = 8 * 1024 * 1024

app = FastAPI(
    title="Lumon API",
    # 本地自托管默认不暴露可枚举全部接口的 OpenAPI 文档。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz", include_in_schema=False)
def healthcheck():
    """供本机容器编排检查进程存活，不返回配置或运行数据。"""
    return {"status": "ok"}


@app.middleware("http")
async def local_access_guard(request: Request, call_next):
    """默认限制 API 只接受本机请求；开放监听时必须显式配置访问令牌。"""
    if request.url.path.startswith("/api"):
        content_length = request.headers.get("content-length", "")
        try:
            if content_length and int(content_length) > _max_request_bytes:
                return JSONResponse(status_code=413, content={"detail": "请求体超过本地服务限制"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "请求头无效"})
        client_host = request.client.host if request.client else ""
        request_host = request.url.hostname or ""
        origin = request.headers.get("origin", "").strip().rstrip("/")
        origin_host = _origin_host(origin) if origin else ""
        if origin and (
            not origin_host
            or (_local_only and not _is_loopback(origin_host))
            or (not _local_only and origin not in _allowed_origins)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "请求来源不在 Lumon 的允许范围内"},
            )
        if _local_only and (
            not _is_loopback(request_host)
            or (not _trust_local_proxy and not _is_loopback(client_host))
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Lumon 默认只允许本机访问，请在服务端配置后再开放远程访问"},
            )
        if not _local_only and not _access_token:
            return JSONResponse(
                status_code=503,
                content={"detail": "远程模式必须配置 LUMON_ACCESS_TOKEN"},
            )
        if _access_token:
            provided = request.headers.get("x-lumon-access-token", "")
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
            if not hmac.compare_digest(provided, _access_token):
                return JSONResponse(status_code=401, content={"detail": "需要有效的 Lumon 访问令牌"})

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-Session-Id",
        "X-API-Key",
        "X-Lumon-Access-Token",
        "Authorization",
    ],
)

app.include_router(router)

# Serve React build output if it exists (production mode)
build_dir = Path(__file__).parent / "frontend" / "dist"
if build_dir.exists():
    app.mount("/", StaticFiles(directory=str(build_dir), html=True), name="frontend")
