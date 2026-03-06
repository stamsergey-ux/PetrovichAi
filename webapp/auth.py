"""JWT-based email authentication for the web app."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

SECRET_KEY = os.getenv("WEBAPP_SECRET", "change-me-in-production-please")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7  # 1 week

# Shared team password for prototype (set via env var)
TEAM_PASSWORD = os.getenv("WEBAPP_PASSWORD", "board2026")

# Allowed emails (comma-separated in env, or hardcoded fallback)
_raw = os.getenv("ALLOWED_EMAILS", "")
ALLOWED_EMAILS: set[str] = set(
    e.strip().lower() for e in _raw.split(",") if e.strip()
)

security = HTTPBearer()


def create_token(email: str) -> str:
    payload = {
        "sub": email.lower(),
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_credentials(email: str, password: str) -> str:
    """Verify email + password, return JWT token."""
    email = email.strip().lower()

    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="Email не в списке разрешённых")

    if password != TEAM_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль")

    return create_token(email)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Extract and validate the JWT token, return email."""
    try:
        payload = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        email: str = payload["sub"]
        return email
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Сессия истекла, войдите снова")
    except Exception:
        raise HTTPException(status_code=401, detail="Недействительный токен")
