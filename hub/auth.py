"""Hermes Hub 鉴权：access_token 换 JWT。"""
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

JWT_SECRET = os.environ.get("HERMES_JWT_SECRET") or secrets.token_urlsafe(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = int(os.environ.get("HERMES_JWT_EXPIRE_SECONDS", "900"))
ACCESS_TOKEN = os.environ.get("HERMES_ACCESS_TOKEN")

bearer = HTTPBearer(auto_error=False)


def issue_access_token() -> str:
    """如果没有配置固定 access_token，Hub 启动时自动生成一个并打印。"""
    return secrets.token_urlsafe(32)


def create_jwt(access_token: str) -> str:
    payload = {
        "sub": access_token[:16],
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRE_SECONDS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def get_current_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired JWT",
        )
    return payload


def init_access_token() -> str:
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        ACCESS_TOKEN = issue_access_token()
    return ACCESS_TOKEN
