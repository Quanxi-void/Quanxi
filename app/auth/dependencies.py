from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId
import jwt
from app.auth.security import decode_token
from app.services.database import get_database
from app.users.repository import touch_user

_bearer = HTTPBearer(auto_error=False)


async def optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials, "access")
        user_id = payload.get("sub")
        if not ObjectId.is_valid(user_id):
            return None
        return await get_database().users.find_one({"_id": ObjectId(user_id), "is_active": True})
    except (jwt.PyJWTError, RuntimeError, ValueError):
        return None


async def current_user(user=Depends(optional_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.", headers={"WWW-Authenticate": "Bearer"})
    await touch_user(str(user["_id"]))
    return user
