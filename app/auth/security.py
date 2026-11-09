import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
import jwt
from app.core.config import settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(subject: str, token_type: str, expires_minutes: int, jti: str | None = None) -> tuple[str, str]:
    token_id = jti or secrets.token_urlsafe(18)
    now = datetime.now(timezone.utc)
    payload = {"sub": subject, "type": token_type, "jti": token_id,
               "iat": now, "exp": now + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), token_id


def decode_token(token: str, expected_type: str = "access") -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("Invalid token type")
    return payload
