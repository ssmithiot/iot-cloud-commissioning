from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import GatewayCredential, utc_now


TOKEN_PATTERN = re.compile(r"^iotcc_gw_([A-Za-z0-9-]{6,64})_([A-Za-z0-9_-]{16,})$")
DEFAULT_GATEWAY_SCOPES = ["edge:heartbeat", "edge:jobs"]


@dataclass(frozen=True)
class GatewayAuthContext:
    gateway_id: str
    credential_id: str
    scopes: list[str]


@dataclass(frozen=True)
class AdminAuthContext:
    authenticated: bool = True


def parse_gateway_token(raw_token: str) -> str:
    match = TOKEN_PATTERN.fullmatch(raw_token)
    if match is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid gateway token")
    return match.group(1)


def hash_gateway_token(raw_token: str) -> str:
    return hmac.new(
        settings.gateway_auth_pepper.encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_gateway_token() -> tuple[str, str]:
    token_prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return token_prefix, f"iotcc_gw_{token_prefix}_{secret}"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid gateway credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _admin_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= utc_now()


def require_gateway_auth(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> GatewayAuthContext:
    if authorization is None:
        raise _unauthorized()

    scheme, separator, raw_token = authorization.partition(" ")
    if separator == "" or scheme.lower() != "bearer" or not raw_token:
        raise _unauthorized()

    token_prefix = parse_gateway_token(raw_token)
    token_hash = hash_gateway_token(raw_token)
    credential = db.scalar(
        select(GatewayCredential).where(
            GatewayCredential.token_prefix == token_prefix,
            GatewayCredential.token_hash == token_hash,
        )
    )

    if credential is None or not hmac.compare_digest(credential.token_hash, token_hash):
        raise _unauthorized()
    if credential.revoked_at is not None or _is_expired(credential.expires_at):
        raise _unauthorized()

    credential.last_used_at = utc_now()
    db.commit()

    return GatewayAuthContext(
        gateway_id=credential.gateway_id,
        credential_id=str(credential.id),
        scopes=list(credential.scopes or []),
    )


def require_admin_auth(authorization: Annotated[str | None, Header()] = None) -> AdminAuthContext:
    if authorization is None:
        raise _admin_unauthorized()

    scheme, separator, raw_token = authorization.partition(" ")
    if separator == "" or scheme.lower() != "bearer" or not raw_token:
        raise _admin_unauthorized()

    if not hmac.compare_digest(raw_token, settings.admin_api_token):
        raise _admin_unauthorized()

    return AdminAuthContext()
