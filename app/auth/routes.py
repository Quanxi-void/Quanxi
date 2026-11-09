from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import jwt
from app.auth.security import hash_password, verify_password, create_token, decode_token
from app.models.common import APIResponse, ResponseMeta
from app.users.schemas import RegisterInput, LoginInput, RefreshInput, TokenPair
from app.users.repository import public_user, log_activity, utcnow
from app.services.database import get_database
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])

def _meta(request: Request):
    return ResponseMeta(request_id=getattr(request.state, "request_id", "unknown"), source="manhwahq-users")

async def _issue_tokens(user: dict, request: Request):
    access, _ = create_token(str(user["_id"]), "access", settings.access_token_minutes)
    refresh, jti = create_token(str(user["_id"]), "refresh", settings.refresh_token_days * 24 * 60)
    await get_database().sessions.insert_one({
        "user_id": str(user["_id"]), "jti": jti,
        "expires_at": utcnow() + timedelta(days=settings.refresh_token_days),
        "created_at": utcnow(), "user_agent": request.headers.get("user-agent", "")[:300],
    })
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"last_activity_at": utcnow()}})
    user["last_activity_at"] = utcnow()
    return TokenPair(access_token=access, refresh_token=refresh,
                     expires_in=settings.access_token_minutes * 60, user=public_user(user))

@router.post("/register", response_model=APIResponse[TokenPair], status_code=201)
async def register(payload: RegisterInput, request: Request):
    db = get_database()
    doc = {
        "email": str(payload.email).lower(), "username": payload.username,
        "display_name": payload.display_name, "password_hash": hash_password(payload.password),
        "bio": None, "avatar": {"kind": "preset", "value": "Avatar-20(1).jpg", "url": f"{settings.api_prefix}/static/avatars/Avatar-20(1).jpg"},
        "is_public": True, "is_active": True, "created_at": utcnow(),
        "last_activity_at": utcnow(), "last_read_at": None,
    }
    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email or username is already registered.")
    doc["_id"] = result.inserted_id
    await log_activity(str(result.inserted_id), "account_created", "Created a ManhwaHQ account")
    tokens = await _issue_tokens(doc, request)
    return APIResponse(data=tokens, meta=_meta(request))

@router.post("/login", response_model=APIResponse[TokenPair])
async def login(payload: LoginInput, request: Request):
    key = payload.email_or_username.strip().lower()
    user = await get_database().users.find_one({"$or": [{"email": key}, {"username": key}], "is_active": True})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")
    await log_activity(str(user["_id"]), "login", "Signed in")
    tokens = await _issue_tokens(user, request)
    return APIResponse(data=tokens, meta=_meta(request))

@router.post("/refresh", response_model=APIResponse[dict])
async def refresh(payload: RefreshInput, request: Request):
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")
    db = get_database()
    session = await db.sessions.find_one({"jti": claims.get("jti"), "user_id": claims.get("sub")})
    if not session:
        raise HTTPException(status_code=401, detail="Refresh session is no longer active.")
    await db.sessions.delete_one({"_id": session["_id"]})
    user = await db.users.find_one({"_id": ObjectId(claims["sub"]), "is_active": True})
    if not user:
        raise HTTPException(status_code=401, detail="Account is unavailable.")
    tokens = await _issue_tokens(user, request)
    return APIResponse(data=tokens, meta=_meta(request))

@router.post("/logout")
async def logout(payload: RefreshInput, request: Request):
    try:
        claims = decode_token(payload.refresh_token, "refresh")
        await get_database().sessions.delete_one({"jti": claims.get("jti"), "user_id": claims.get("sub")})
    except jwt.PyJWTError:
        pass
    return APIResponse(data={"logged_out": True}, meta=_meta(request))
