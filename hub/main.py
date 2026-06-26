"""Hermes Hub 入口。"""
import logging
import os

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import ACCESS_TOKEN, create_jwt, get_current_token, init_access_token, JWT_EXPIRE_SECONDS
from .hermes_runner import HERMES_BINARY, HERMES_CONTAINER, run_hermes
from .sessions_router import router as sessions_router
from .sse_manager import sse_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hermes_hub")


class AuthBody(BaseModel):
    accessToken: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    token = init_access_token()
    logger.info("Hermes Hub 启动")
    logger.info("使用 Hermes: binary=%s container=%s", HERMES_BINARY, HERMES_CONTAINER or "(本地)")
    logger.info("Access Token: %s", token)
    yield
    await sse_manager.shutdown()


app = FastAPI(title="Hermes Hub", version="1.0.0", lifespan=lifespan)

allowed_origins = os.environ.get("HERMES_CORS_ORIGINS", "")
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in allowed_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/health")
async def health():
    code, stdout, stderr = await run_hermes(["--version"], timeout=15)
    version = stdout.strip().split("\n")[0] if code == 0 and stdout else None
    return {
        "ok": code == 0,
        "version": version,
        "error": stderr[:200] if code != 0 else None,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/auth")
async def auth(body: AuthBody):
    if not ACCESS_TOKEN:
        raise Exception("Access token not initialized")
    if body.accessToken != ACCESS_TOKEN:
        return {"ok": False, "error": "Invalid access token"}, 401
    token = create_jwt(body.accessToken)
    return {"ok": True, "token": token, "expires_in": JWT_EXPIRE_SECONDS}


@app.get("/api/events")
async def events(token: dict = Depends(get_current_token)):
    async def generator():
        async for data in sse_manager.subscribe():
            yield data
    return StreamingResponse(generator(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


app.include_router(sessions_router)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HERMES_HOST", "127.0.0.1")
    port = int(os.environ.get("HERMES_PORT", "9800"))
    kwargs = {}
    ssl_key = os.environ.get("HERMES_SSL_KEYFILE")
    ssl_cert = os.environ.get("HERMES_SSL_CERTFILE")
    if ssl_key and ssl_cert:
        kwargs["ssl_keyfile"] = ssl_key
        kwargs["ssl_certfile"] = ssl_cert
        logger.info("HTTPS 模式: key=%s cert=%s", ssl_key, ssl_cert)
    uvicorn.run(app, host=host, port=port, **kwargs)
